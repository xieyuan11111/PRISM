"""Europe PMC fullTextXML intake (offline, injected HTTP).

Real-run findings in ``docs/case-driven-auto-research-loop.md`` §12.3: the
PMC article *page* fetch returned site navigation as the body, wrote the
fetch time as ``published_at`` for a 2017 paper, and mislabelled the
material as news.  Academic sources must be ingested through the reliable
metadata/full-text APIs instead: authoritative publication metadata, clean
article body, honest material type/access level — always through the
existing SourceIntake/Ingestion path, never bypassing it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.api import PrismAPI
from prism.config import PathConfig, PrismConfig, SourceConfig
from prism.ingestion import IngestionService
from prism.sources import (
    CrossrefClient,
    EuropePmcClient,
    FailureKind,
    HttpResponse,
    OpenAlexClient,
    ScholarlyMetadataClient,
    SourceFetchError,
    SourceItem,
    SourceService,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)
PMCID = "PMC5331289"
PMC_URL = f"https://pmc.ncbi.nlm.nih.gov/articles/{PMCID}/"
SEARCH_URL = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    f"?query=PMCID%3A{PMCID}&format=json&resultType=core"
)
FULLTEXT_URL = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/"
    f"{PMCID}/fullTextXML"
)

METADATA = {
    "version": "6.0",
    "hitCount": 1,
    "resultList": {
        "result": [
            {
                "pmid": "28074861",
                "pmcid": PMCID,
                "title": "Long noncoding RNA lifecycle",
                "authorString": "Statello L, Guo CJ.",
                "journalTitle": "Cell",
                "pubYear": "2017",
                "firstPublicationDate": "2017-01-12",
                "doi": "10.1016/j.cell.2016.12.035",
                "abstractText": "A study of lncRNA mechanisms.",
            }
        ]
    },
}

JATS_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <article-meta>
      <title-group><article-title>Long noncoding RNA lifecycle</article-title></title-group>
    </article-meta>
  </front>
  <body>
    <sec>
      <title>Introduction</title>
      <p>Long noncoding RNAs regulate gene expression across <italic>many</italic> cell types.</p>
      <sec>
        <title>Mechanisms</title>
        <p>They act through chromatin, transcription and post-transcriptional routes.</p>
      </sec>
    </sec>
    <sec>
      <title>Outlook</title>
      <p>The field is moving toward single-cell resolution.</p>
    </sec>
  </body>
  <back><ref-list><ref>Some reference</ref></ref-list></back>
</article>
"""

NAVIGATED_PAGE = """<!DOCTYPE html>
<html><head><title>PMC5331289</title></head><body>
<nav><p>Skip to main content</p><p>Official websites use .gov</p></nav>
<article><p>Long noncoding RNA lifecycle — full text would be here.</p></article>
</body></html>
"""


class FakeGetter:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    async def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append(url)
        outcome = self.routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


def ok(url: str, body: str, content_type: str = "application/xml") -> HttpResponse:
    return HttpResponse(url=url, status=200, body=body, content_type=content_type)


def json_ok(url: str, payload: object) -> HttpResponse:
    import json

    return HttpResponse(
        url=url, status=200, body=json.dumps(payload), content_type="application/json"
    )


def run(coro):
    return asyncio.run(coro)


def make_europepmc(routes: dict[str, object]) -> EuropePmcClient:
    return EuropePmcClient(FakeGetter(routes))


def make_scholarly(getter: FakeGetter) -> ScholarlyMetadataClient:
    return ScholarlyMetadataClient(
        CrossrefClient(getter),
        OpenAlexClient(getter),
        EuropePmcClient(getter),
        clock=lambda: NOW,
    )


# --- EuropePmcClient.fetch_fulltext ------------------------------------------


def test_fetch_fulltext_returns_clean_body_with_authoritative_metadata():
    client = make_europepmc(
        {SEARCH_URL: json_ok(SEARCH_URL, METADATA), FULLTEXT_URL: ok(FULLTEXT_URL, JATS_BODY)}
    )
    record = run(client.fetch_fulltext(PMCID, retrieved_at=NOW))

    assert record.access_level == "fulltext"
    assert record.pmcid == PMCID
    # published_at comes from Europe PMC metadata, never from the fetch time.
    assert record.published_at == datetime(2017, 1, 12, tzinfo=UTC)
    body = record.body_markdown
    assert body is not None
    assert "They act through chromatin, transcription and post-transcriptional routes." in body
    assert "## Mechanisms" in body
    # Inline JATS markup is flattened to plain text.
    assert "regulate gene expression across many cell types." in body
    assert "<italic>" not in body
    # Back matter and front matter navigation are not article prose.
    assert "Some reference" not in body
    assert "Skip to main content" not in body


def test_fetch_fulltext_item_marks_academic_type_and_retrieval_level():
    client = make_europepmc(
        {SEARCH_URL: json_ok(SEARCH_URL, METADATA), FULLTEXT_URL: ok(FULLTEXT_URL, JATS_BODY)}
    )
    record = run(client.fetch_fulltext(PMCID, retrieved_at=NOW))
    item = record.to_source_item()

    assert isinstance(item, SourceItem)
    assert item.type == "academic"
    assert item.access_level == "fulltext"
    assert item.retrieval_level == "fulltext"
    assert item.published_at == datetime(2017, 1, 12, tzinfo=UTC)
    assert item.content == record.body_markdown
    assert item.pmcid == PMCID
    assert item.doi == "10.1016/j.cell.2016.12.035"


def test_fetch_fulltext_without_open_access_body_stays_abstract_only():

    not_found = HttpResponse(url=FULLTEXT_URL, status=404, body="not found")
    client = make_europepmc(
        {SEARCH_URL: json_ok(SEARCH_URL, METADATA), FULLTEXT_URL: not_found}
    )
    record = run(client.fetch_fulltext(PMCID, retrieved_at=NOW))

    assert record.access_level == "abstract_only"
    assert record.body_markdown is None
    assert record.abstract == "A study of lncRNA mechanisms."
    assert record.published_at == datetime(2017, 1, 12, tzinfo=UTC)


def test_fetch_fulltext_rejects_dtd_and_entity_declarations():
    malicious = JATS_BODY.replace(
        "<article ",
        "<!DOCTYPE article [<!ENTITY xxe SYSTEM \"file:///C:/secrets\">]>\n<article ",
    ).replace("many", "&xxe;")
    client = make_europepmc(
        {SEARCH_URL: json_ok(SEARCH_URL, METADATA), FULLTEXT_URL: ok(FULLTEXT_URL, malicious)}
    )
    with pytest.raises(SourceFetchError) as excinfo:
        run(client.fetch_fulltext(PMCID, retrieved_at=NOW))
    assert excinfo.value.kind is FailureKind.PARSE


def test_scholarly_client_routes_pmc_urls_through_fulltext():
    getter = FakeGetter(
        {SEARCH_URL: json_ok(SEARCH_URL, METADATA), FULLTEXT_URL: ok(FULLTEXT_URL, JATS_BODY)}
    )
    item = run(make_scholarly(getter).fetch(PMC_URL))

    assert item.access_level == "fulltext"
    assert item.published_at == datetime(2017, 1, 12, tzinfo=UTC)
    assert item.type == "academic"
    assert item.content is not None and "Mechanisms" in item.content
    assert FULLTEXT_URL in getter.calls


# --- facade routing: scholarly intake before the page fetch ------------------


class StubStore:
    def __init__(self) -> None:
        self.indexed: list[object] = []

    def index_file(self, path):
        self.indexed.append(path)
        return None

    def get(self, source_id):
        return None

    def search(self, criteria, *, limit, offset):
        return []


class StubGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("timeline is not part of the fetch path")

    async def add_case(self, case, **bundle):
        raise AssertionError("add_case is not part of the fetch path")


class FakePipeline:
    async def run_material(self, result, *, correlation_id=None):
        from prism.pipeline import PipelineRun

        return PipelineRun(
            material_id=result.material.id,
            status="completed",
            started_at=NOW,
            finished_at=NOW,
        )


class FakeBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event) -> None:
        self.events.append(event)


def make_api(tmp_path: Path, getter: FakeGetter, scholarly) -> PrismAPI:
    paths = PathConfig(
        data_dir=tmp_path / "data",
        raw_dir=tmp_path / "raw",
        corpus_dir=tmp_path / "corpus",
    ).resolve(tmp_path)
    config = PrismConfig(sources=SourceConfig(("pmc.ncbi.nlm.nih.gov",)))
    return PrismAPI(
        IngestionService(paths),
        StubStore(),
        StubGraph(),
        FakeBus(),
        source_service=SourceService(config, getter=getter, clock=lambda: NOW),
        pipeline_service=FakePipeline(),
        source_raw_dir=tmp_path / "raw",
        scholarly_metadata_client=scholarly,
    )


def test_fetch_source_prefers_fulltext_intake_for_pmc_article_pages(tmp_path):
    getter = FakeGetter(
        {
            SEARCH_URL: json_ok(SEARCH_URL, METADATA),
            FULLTEXT_URL: ok(FULLTEXT_URL, JATS_BODY),
            PMC_URL: ok(PMC_URL, NAVIGATED_PAGE, "text/html"),
        }
    )
    api = make_api(tmp_path, getter, make_scholarly(getter))

    report = run(api.fetch_source(PMC_URL, kind="page"))

    (item,) = report.items
    # The navigated site page was never fetched: the reliable Europe PMC
    # metadata + fullTextXML intake replaced the polluted page extraction.
    assert PMC_URL not in getter.calls
    assert item.title == "Long noncoding RNA lifecycle"
    assert item.access_level == "fulltext"


def test_fetch_source_fulltext_item_ingests_with_metadata_timestamp(tmp_path):
    """The corpus material must carry the authoritative publication date and
    academic type — not the fetch time and a 'news' label (docs §12.3)."""
    from prism.ingestion import IngestionService as Ingestion

    getter = FakeGetter(
        {
            SEARCH_URL: json_ok(SEARCH_URL, METADATA),
            FULLTEXT_URL: ok(FULLTEXT_URL, JATS_BODY),
        }
    )
    paths = PathConfig(
        data_dir=tmp_path / "data",
        raw_dir=tmp_path / "raw",
        corpus_dir=tmp_path / "corpus",
    ).resolve(tmp_path)
    captured = {}
    real_ingest = Ingestion.ingest

    class SpyIngestion(Ingestion):
        def ingest(self, path, metadata=None):
            captured.update(metadata or {})
            return real_ingest(self, path, metadata)

    config = PrismConfig(sources=SourceConfig(("pmc.ncbi.nlm.nih.gov",)))
    api = PrismAPI(
        SpyIngestion(paths),
        StubStore(),
        StubGraph(),
        FakeBus(),
        source_service=SourceService(config, getter=getter, clock=lambda: NOW),
        pipeline_service=FakePipeline(),
        source_raw_dir=tmp_path / "raw",
        scholarly_metadata_client=make_scholarly(getter),
    )
    run(api.fetch_source(PMC_URL, kind="auto"))

    assert captured["type"] == "academic"
    assert captured["published_at"] == datetime(2017, 1, 12, tzinfo=UTC)
    assert captured["fetched_at"] == NOW
    assert captured["access_level"] == "fulltext"
    assert captured["pmcid"] == PMCID


def test_fetch_source_falls_back_to_page_fetch_when_fulltext_unavailable(tmp_path):
    """No Europe PMC full text (or metadata) → the ordinary whitelisted page
    fetch still runs; nothing is silently dropped and no wall is bypassed."""
    import json as _json

    getter = FakeGetter(
        {
            SEARCH_URL: HttpResponse(url=SEARCH_URL, status=200, body=_json.dumps({
                "version": "6.0", "hitCount": 0, "resultList": {"result": []}
            }), content_type="application/json"),
            PMC_URL: ok(PMC_URL, NAVIGATED_PAGE, "text/html"),
        }
    )
    api = make_api(tmp_path, getter, make_scholarly(getter))

    report = run(api.fetch_source(PMC_URL, kind="page"))

    (item,) = report.items
    assert PMC_URL in getter.calls  # the page fetch happened as before
    assert item.material_id.startswith("mat_")
