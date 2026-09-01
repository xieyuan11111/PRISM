"""Focused integration tests wiring public source collection into the app.

``PrismAPI.fetch_source``/``fetch_sources`` must move every collected
:class:`~prism.sources.SourceItem` through the auditable path — safe raw
spool file → :class:`~prism.ingestion.IngestionService` →
:class:`~prism.pipeline.PipelineService` — never bypassing ingestion; the
runtime must assemble the source/extraction/pipeline services behind an
explicitly injected HTTP getter (never an implicit network client); and the
``prism fetch``/``prism fetch-all`` shells must only delegate to the facade
and render deterministic JSON.  Everything stays offline: HTTP traffic is
faked, the CLI receives injected facades, and LLM transports are fakes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import re

import pytest

from prism.api import PrismAPI
from prism.api.fetching import (
    SPOOL_DIRNAME,
    SourceBatchReport,
    SourceFetchReport,
    SourceItemReport,
    SourceURLFailure,
    spool_source_item,
)
from prism.cli import build_parser, handle_fetch, handle_fetch_all, main
from prism.config import (
    LLMConfig,
    LLMProviderConfig,
    PathConfig,
    PrismConfig,
    SourceConfig,
)
from prism.extraction import ExtractionService
from prism.ingestion import IngestionService
from prism.llm import TransportResponse
from prism.pipeline import PipelineRun, PipelineService
from prism.runtime import OfflineExtractor, create_runtime
from prism.sources import (
    FailureKind,
    HttpResponse,
    SourceFetchError,
    SourceItem,
    SourceService,
)


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
PUBLISHED = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

FEED_URL = "https://example.gov/feeds/news.xml"
PAGE_URL = "https://example.gov/announcements/policy.html"
MISSING_URL = "https://example.gov/feeds/missing.xml"
BARE_FEED_URL = "https://example.gov/feeds/bare.xml"
BLOCKED_URL = "https://blocked.example.net/feed"
WHITELIST = ("example.gov", "example.org")

RSS_BODY = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Gov News</title>
    <item>
      <title>Housing policy updated</title>
      <link>https://example.gov/news/housing-policy-updated</link>
      <pubDate>Mon, 24 Aug 2026 08:00:00 +0000</pubDate>
      <description>The housing policy was revised.</description>
    </item>
    <item>
      <title>Ministry answers questions</title>
      <link>https://example.gov/news/ministry-answers</link>
      <pubDate>Tue, 25 Aug 2026 09:30:00 +0000</pubDate>
      <description>Answers to press questions.</description>
    </item>
  </channel>
</rss>
"""

BARE_FEED_BODY = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example Gov News</title>
  <item><title>Bare entry</title><link>https://example.gov/news/bare</link></item>
</channel></rss>
"""

PAGE_BODY = """\
<!DOCTYPE html>
<html>
<head><title>Policy announcement page</title></head>
<body>
  <article><p>The ministry announced the new measures today.</p></article>
</body>
</html>
"""

SPOOL_NAME_PATTERN = re.compile(r"source-[0-9a-f]{32}\.md")


def ok(url: str, body: str) -> HttpResponse:
    return HttpResponse(url=url, status=200, body=body)


class FakeGetter:
    """Injectable async HTTP transport with per-URL canned outcomes."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes: dict[str, object] = dict(routes or {})
        self.calls: list[str] = []

    async def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append(url)
        outcome = self.routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


class FakePipeline:
    """Injectable pipeline service recording every run_material call."""

    def __init__(self, *, explode: bool = False) -> None:
        self.explode = explode
        self.calls: list[tuple[object, str | None]] = []

    async def run_material(self, result, *, correlation_id=None):
        self.calls.append((result, correlation_id))
        if self.explode:
            raise RuntimeError("pipeline exploded")
        return PipelineRun(
            material_id=result.material.id,
            status="completed",
            started_at=NOW,
            finished_at=NOW,
        )


class FakeBus:
    """Injectable running event bus recording published events."""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event) -> None:
        self.events.append(event)


class StubIngestion:
    def ingest(self, path, metadata=None):
        raise AssertionError("not part of this path")


class StubStore:
    def index_file(self, path):
        raise AssertionError("indexing belongs to the pipeline stage")

    def search(self, criteria, *, limit, offset):
        return []


class StubGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("timeline is not part of the fetch path")

    async def add_case(self, case, **bundle):
        raise AssertionError("add_case is not part of the fetch path")


class OfflineTransport:
    """LLM transport fake that never leaves the process."""

    async def complete(self, *, provider, api_key, payload, timeout):
        return TransportResponse("offline answer")

    async def test_connection(self, *, provider, api_key, timeout):
        return True


def make_ingestion(tmp_path: Path) -> IngestionService:
    return IngestionService(
        PathConfig(data_dir=tmp_path / "data", raw_dir=Path("raw"), corpus_dir=Path("corpus"))
    )


def make_source_service(getter: FakeGetter) -> SourceService:
    config = PrismConfig(sources=SourceConfig(whitelist=WHITELIST))
    return SourceService(config, getter=getter, clock=lambda: NOW)


def make_fetch_api(
    tmp_path: Path,
    getter: FakeGetter,
    *,
    pipeline: object | None = None,
    bus: object | None = None,
    raw_dir: Path | None = None,
    with_source: bool = True,
    with_pipeline: bool = True,
    with_raw_dir: bool = True,
) -> PrismAPI:
    return PrismAPI(
        make_ingestion(tmp_path),
        StubStore(),
        StubGraph(),
        bus or FakeBus(),
        source_service=make_source_service(getter) if with_source else None,
        pipeline_service=pipeline if pipeline is not None else (FakePipeline() if with_pipeline else None),
        source_raw_dir=(raw_dir if raw_dir is not None else tmp_path / "raw") if with_raw_dir else None,
    )


def spool_names(tmp_path: Path) -> set[str]:
    return {path.name for path in (tmp_path / "raw" / SPOOL_DIRNAME).iterdir()}


# --- spool: safe raw writing -------------------------------------------------


def test_spool_names_are_stable_and_traversal_free(tmp_path: Path) -> None:
    item = SourceItem(
        title="Housing policy updated",
        source="example.gov",
        fetched_at=NOW,
        link="https://example.gov/news/housing-policy-updated",
        summary="The housing policy was revised.",
    )
    other_link = SourceItem(
        title="Other",
        source="example.gov",
        fetched_at=NOW,
        link="https://example.gov/news/other",
        summary="Different summary.",
    )

    first = spool_source_item(item, tmp_path / "raw" / SPOOL_DIRNAME)
    second = spool_source_item(item, tmp_path / "other" / SPOOL_DIRNAME)
    third = spool_source_item(other_link, tmp_path / "raw" / SPOOL_DIRNAME)

    assert first.name == second.name  # stable across directories and calls
    assert first.name != third.name  # distinct items get distinct files
    assert SPOOL_NAME_PATTERN.fullmatch(first.name)
    assert first.parent == tmp_path / "raw" / SPOOL_DIRNAME
    assert first.read_text(encoding="utf-8") == "The housing policy was revised."
    # atomic write leaves no temporary residue behind
    assert spool_names(tmp_path) == {first.name, third.name}


def test_spool_creates_directories_and_overwrites_atomically(tmp_path: Path) -> None:
    spool_dir = tmp_path / "raw" / "nested" / SPOOL_DIRNAME
    item = SourceItem(
        title="Bare",
        source="example.gov",
        fetched_at=NOW,
        link="https://example.gov/news/bare",
    )

    path = spool_source_item(item, spool_dir)

    assert spool_dir.is_dir()  # created on demand
    assert path.read_text(encoding="utf-8") == ""  # neither content nor summary

    stale = spool_dir / path.name
    stale.write_text("stale contents", encoding="utf-8")
    again = spool_source_item(item, spool_dir)
    assert again == path
    assert again.read_text(encoding="utf-8") == ""
    assert {p.name for p in spool_dir.iterdir()} == {path.name}


def test_spool_prefers_content_over_summary(tmp_path: Path) -> None:
    spool_dir = tmp_path / "raw" / SPOOL_DIRNAME
    with_content = SourceItem(
        title="A",
        source="example.gov",
        fetched_at=NOW,
        link="https://example.gov/a",
        summary="short summary",
        content="full body text",
    )
    summary_only = SourceItem(
        title="B",
        source="example.gov",
        fetched_at=NOW,
        link="https://example.gov/b",
        summary="summary fallback",
    )

    assert spool_source_item(with_content, spool_dir).read_text(encoding="utf-8") == "full body text"
    assert (
        spool_source_item(summary_only, spool_dir).read_text(encoding="utf-8")
        == "summary fallback"
    )


# --- PrismAPI.fetch_source ---------------------------------------------------


def test_fetch_source_spools_ingests_and_processes_each_item(tmp_path: Path) -> None:
    getter = FakeGetter({FEED_URL: ok(FEED_URL, RSS_BODY)})
    pipeline = FakePipeline()
    bus = FakeBus()
    api = make_fetch_api(tmp_path, getter, pipeline=pipeline, bus=bus)

    report = asyncio.run(api.fetch_source(FEED_URL))

    assert isinstance(report, SourceFetchReport)
    assert report.url == FEED_URL
    assert report.fetched_at == NOW
    assert report.duplicate_keys == ()
    assert [item.title for item in report.items] == [
        "Housing policy updated",
        "Ministry answers questions",
    ]

    first, second = report.items
    assert isinstance(first, SourceItemReport)
    assert first.source == "example.gov"
    assert first.link == "https://example.gov/news/housing-policy-updated"
    assert first.material_id.startswith("mat_")
    assert first.spool_path.parent == tmp_path / "raw" / SPOOL_DIRNAME
    assert first.raw_path.parent == tmp_path / "raw"
    assert first.raw_path != first.spool_path  # ingestion keeps its own raw copy
    assert first.corpus_path.parent == tmp_path / "corpus"
    assert first.corpus_path.is_file()
    assert first.raw_path.is_file()
    assert first.spool_path.is_file()
    assert first.pipeline is not None and first.pipeline.status == "completed"
    assert second.pipeline is not None and second.pipeline.status == "completed"

    # the corpus is produced by IngestionService, never hand-written here
    corpus_text = first.corpus_path.read_text(encoding="utf-8")
    assert corpus_text.startswith("---\n")
    assert "source: \"example.gov\"" in corpus_text

    # feed items carry no content: the summary was spooled as the raw body
    assert first.spool_path.read_text(encoding="utf-8") == "The housing policy was revised."
    assert spool_names(tmp_path) == {first.spool_path.name, second.spool_path.name}

    # every item went through ingestion into the pipeline, with full metadata
    assert [call[1] for call in pipeline.calls] == [None, None]
    ingested = [call[0].material for call in pipeline.calls]
    assert ingested[0].title == "Housing policy updated"
    assert ingested[0].url == first.link
    assert ingested[0].published_at == PUBLISHED
    assert ingested[0].fetched_at == NOW
    assert ingested[0].content == "The housing policy was revised."

    # one completion event per ingested material keeps the bus in the loop
    assert [event.event_type for event in bus.events] == ["material.ingested"] * 2
    assert bus.events[0].correlation_id == first.material_id
    assert bus.events[0].payload["material_id"] == first.material_id
    assert bus.events[0].payload["url"] == first.link
    assert bus.events[0].payload["pipeline_status"] == "completed"


def test_fetch_source_page_item_uses_full_content(tmp_path: Path) -> None:
    getter = FakeGetter({PAGE_URL: ok(PAGE_URL, PAGE_BODY)})
    pipeline = FakePipeline()
    api = make_fetch_api(tmp_path, getter, pipeline=pipeline)

    report = asyncio.run(api.fetch_source(PAGE_URL))

    (item,) = report.items
    assert item.title == "Policy announcement page"
    assert "The ministry announced the new measures today." in item.spool_path.read_text(
        encoding="utf-8"
    )
    assert pipeline.calls[0][0].material.content.startswith(
        "The ministry announced the new measures today."
    )


def test_fetch_source_process_false_ingests_without_pipeline(tmp_path: Path) -> None:
    getter = FakeGetter({PAGE_URL: ok(PAGE_URL, PAGE_BODY)})
    pipeline = FakePipeline()
    bus = FakeBus()
    api = make_fetch_api(tmp_path, getter, pipeline=pipeline, bus=bus)

    report = asyncio.run(api.fetch_source(PAGE_URL, process=False))

    (item,) = report.items
    assert item.pipeline is None  # not processed: no fabricated pipeline run
    assert item.corpus_path.is_file()
    assert pipeline.calls == []
    assert [event.event_type for event in bus.events] == ["material.ingested"]
    assert bus.events[0].payload["pipeline_status"] is None


def test_fetch_source_is_idempotent_through_dedup(tmp_path: Path) -> None:
    getter = FakeGetter({FEED_URL: ok(FEED_URL, RSS_BODY)})
    pipeline = FakePipeline()
    api = make_fetch_api(tmp_path, getter, pipeline=pipeline)

    first = asyncio.run(api.fetch_source(FEED_URL))
    second = asyncio.run(api.fetch_source(FEED_URL))

    assert len(first.items) == 2
    assert second.items == ()
    assert len(second.duplicate_keys) == 2
    assert all(key.startswith("link:") for key in second.duplicate_keys)
    assert spool_names(tmp_path) == {item.spool_path.name for item in first.items}
    assert len(pipeline.calls) == 2  # nothing reprocessed
    corpus_files = {item.corpus_path.name for item in first.items}
    assert {p.name for p in (tmp_path / "corpus").iterdir()} == corpus_files


def test_fetch_source_failures_are_raised_not_swallowed(tmp_path: Path) -> None:
    getter = FakeGetter(
        {MISSING_URL: HttpResponse(url=MISSING_URL, status=404, body="")}
    )

    blocked = make_fetch_api(tmp_path, getter)
    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(blocked.fetch_source(BLOCKED_URL))
    assert excinfo.value.kind is FailureKind.BLOCKED
    assert getter.calls == []  # whitelist refusal happens before any transport

    missing = make_fetch_api(tmp_path, getter)
    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(missing.fetch_source(MISSING_URL))
    assert excinfo.value.kind is FailureKind.HTTP_STATUS

    no_source = make_fetch_api(tmp_path, getter, with_source=False)
    with pytest.raises(ValueError, match="source_service is required"):
        asyncio.run(no_source.fetch_source(FEED_URL))

    no_pipeline = make_fetch_api(tmp_path, getter, with_pipeline=False)
    with pytest.raises(ValueError, match="pipeline_service is required"):
        asyncio.run(no_pipeline.fetch_source(FEED_URL))

    no_raw_dir = make_fetch_api(tmp_path, getter, with_raw_dir=False)
    with pytest.raises(ValueError, match="source_raw_dir is required"):
        asyncio.run(no_raw_dir.fetch_source(FEED_URL))


def test_fetch_source_pipeline_failure_propagates(tmp_path: Path) -> None:
    getter = FakeGetter({PAGE_URL: ok(PAGE_URL, PAGE_BODY)})
    api = make_fetch_api(tmp_path, getter, pipeline=FakePipeline(explode=True))

    with pytest.raises(RuntimeError, match="pipeline exploded"):
        asyncio.run(api.fetch_source(PAGE_URL))


def test_fetch_source_rejects_items_ingestion_cannot_normalize(tmp_path: Path) -> None:
    getter = FakeGetter({BARE_FEED_URL: ok(BARE_FEED_URL, BARE_FEED_BODY)})
    api = make_fetch_api(tmp_path, getter)

    with pytest.raises(ValueError, match="extracted content must not be empty"):
        asyncio.run(api.fetch_source(BARE_FEED_URL))


# --- PrismAPI.fetch_sources --------------------------------------------------


def test_fetch_sources_retains_every_failure_without_faking_success(
    tmp_path: Path,
) -> None:
    getter = FakeGetter(
        {
            FEED_URL: ok(FEED_URL, RSS_BODY),
            MISSING_URL: HttpResponse(url=MISSING_URL, status=404, body=""),
            BARE_FEED_URL: ok(BARE_FEED_URL, BARE_FEED_BODY),
        }
    )
    pipeline = FakePipeline()
    api = make_fetch_api(tmp_path, getter, pipeline=pipeline)

    batch = asyncio.run(
        api.fetch_sources([FEED_URL, MISSING_URL, BLOCKED_URL, BARE_FEED_URL])
    )

    assert isinstance(batch, SourceBatchReport)
    assert [report.url for report in batch.reports] == [FEED_URL]
    assert len(batch.reports[0].items) == 2
    assert isinstance(batch.failures[0], SourceURLFailure)
    assert [(failure.url, failure.kind) for failure in batch.failures] == [
        (MISSING_URL, "http_status"),
        (BLOCKED_URL, "blocked"),
        (BARE_FEED_URL, "ValueError"),
    ]
    assert all(failure.detail for failure in batch.failures)
    assert len(pipeline.calls) == 2  # only the successful feed reached the pipeline
    # the uningestable bare item still left its raw spool file for audit
    assert len(spool_names(tmp_path)) == 3


def test_fetch_sources_validates_dependencies_even_when_empty(tmp_path: Path) -> None:
    api = make_fetch_api(tmp_path, FakeGetter(), with_source=False)

    with pytest.raises(ValueError, match="source_service is required"):
        asyncio.run(api.fetch_sources([]))


def test_legacy_constructor_shape_is_unchanged() -> None:
    api = PrismAPI(StubIngestion(), StubStore(), StubGraph(), FakeBus())

    assert asyncio.run(api.search("anything")) == []
    with pytest.raises(ValueError, match="source_service is required"):
        asyncio.run(api.fetch_source(FEED_URL))


def test_invalid_optional_fetch_dependencies_are_rejected_up_front() -> None:
    def base_kwargs() -> dict[str, object]:
        return {
            "source_service": make_source_service(FakeGetter()),
            "pipeline_service": FakePipeline(),
            "source_raw_dir": Path("raw"),
        }

    with pytest.raises(TypeError, match="source_service must provide fetch"):
        PrismAPI(
            StubIngestion(), StubStore(), StubGraph(), FakeBus(),
            source_service=object(),
        )
    with pytest.raises(TypeError, match="pipeline_service must provide run_material"):
        PrismAPI(
            StubIngestion(), StubStore(), StubGraph(), FakeBus(),
            pipeline_service=object(),
        )
    with pytest.raises(TypeError, match="source_raw_dir must be path-like"):
        PrismAPI(StubIngestion(), StubStore(), StubGraph(), FakeBus(), **{**base_kwargs(), "source_raw_dir": 123})


# --- runtime assembly ----------------------------------------------------------


def test_runtime_assembles_source_extraction_and_pipeline_services(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    PrismConfig(sources=SourceConfig(whitelist=("example.gov",))).save(config_path)
    getter = FakeGetter({FEED_URL: ok(FEED_URL, RSS_BODY)})

    async def exercise():
        runtime = await create_runtime(config_path, http_getter=getter)
        try:
            assert isinstance(runtime.source_service, SourceService)
            assert runtime.sources is runtime.source_service
            assert isinstance(runtime.pipeline_service, PipelineService)
            assert runtime.pipeline is runtime.pipeline_service
            assert isinstance(runtime.extraction_service, OfflineExtractor)
            assert runtime.extraction is runtime.extraction_service

            report = await runtime.api.fetch_source(FEED_URL)

            assert len(report.items) == 2
            for item in report.items:
                run = item.pipeline
                assert run is not None and run.status == "completed"
                assert [stage.name for stage in run.stages] == [
                    "index",
                    "extract",
                    "graph",
                ]
                assert run.stages[0].result.entry.url == item.link  # really indexed
                assert any(
                    "no LLM router" in warning
                    for warning in run.stages[1].result.warnings
                )
                assert run.stages[2].status == "skipped"  # no case: no fabricated graph
                assert item.corpus_path.is_file()
                assert item.raw_path.is_file()

            spool_dir = runtime.paths.raw_dir / SPOOL_DIRNAME
            assert {p.name for p in spool_dir.iterdir()} == {
                item.spool_path.name for item in report.items
            }

            hits = await runtime.api.search("housing")
            assert [hit.source_id for hit in hits] == [report.items[0].material_id]
            assert hits[0].url == "https://example.gov/news/housing-policy-updated"
        finally:
            await runtime.close()
            await runtime.close()  # idempotent shutdown

        assert runtime.evidence_store._connection is None

    asyncio.run(exercise())


def test_runtime_without_injected_getter_stays_offline(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))

    async def exercise():
        runtime = await create_runtime()
        try:
            assert runtime.source_service is None  # no implicit network client
            with pytest.raises(ValueError, match="source_service is required"):
                await runtime.api.fetch_source(FEED_URL)
        finally:
            await runtime.close()

    asyncio.run(exercise())


def test_runtime_with_llm_router_assembles_real_extraction_service(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    PrismConfig(
        llm=LLMConfig(
            providers={
                "primary": LLMProviderConfig(
                    model="provider/model-v1",
                    base_url="https://llm.example.test/v1",
                    api_key_env="PRISM_TEST_API_KEY",
                )
            },
            task_roles={"extract": "primary"},
        ),
        sources=SourceConfig(whitelist=("example.gov",)),
    ).save(config_path)

    async def exercise():
        runtime = await create_runtime(
            config_path, llm_transport=OfflineTransport(), http_getter=FakeGetter()
        )
        try:
            assert isinstance(runtime.extraction_service, ExtractionService)
            assert isinstance(runtime.pipeline_service, PipelineService)
            assert isinstance(runtime.source_service, SourceService)
        finally:
            await runtime.close()

    asyncio.run(exercise())


# --- CLI: fetch and fetch-all --------------------------------------------------


def make_cli_report() -> SourceFetchReport:
    run = PipelineRun(
        material_id="mat_cli00000000000000000000",
        status="completed",
        started_at=NOW,
        finished_at=NOW,
    )
    item = SourceItemReport(
        title="Housing policy updated",
        source="example.gov",
        link="https://example.gov/news/housing-policy-updated",
        material_id="mat_cli00000000000000000000",
        spool_path=Path("raw/spool/source-cli.md"),
        raw_path=Path("raw/mat_cli.md"),
        corpus_path=Path("corpus/Housing-policy-updated-mat_cli.md"),
        pipeline=run,
    )
    return SourceFetchReport(url=FEED_URL, fetched_at=NOW, items=(item,))


def make_batch_report() -> SourceBatchReport:
    return SourceBatchReport(
        reports=(make_cli_report(),),
        failures=(
            SourceURLFailure(url=MISSING_URL, kind="http_status", detail="HTTP 404"),
        ),
    )


class FakeFetchAPI:
    """Injected CLI facade with canned fetch outcomes and call recording."""

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch_source(self, url, *, kind="auto", process=True):
        self.calls.append(("fetch_source", (url, kind, process)))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def fetch_sources(self, urls, *, kind="auto", process=True):
        self.calls.append(("fetch_sources", (tuple(urls), kind, process)))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def run_cli(argv, api):
    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(main(argv, api=api, stdout=stdout, stderr=stderr))
    return status, stdout.getvalue(), stderr.getvalue()


def test_parser_exposes_fetch_and_fetch_all_subcommands() -> None:
    parser = build_parser()

    fetch = parser.parse_args(["fetch", FEED_URL])
    assert fetch.handler is handle_fetch
    assert fetch.url == FEED_URL
    assert fetch.kind == "auto"
    assert fetch.no_process is False

    fetch_all = parser.parse_args(["fetch-all", "urls.json"])
    assert fetch_all.handler is handle_fetch_all
    assert fetch_all.urls == "urls.json"
    assert fetch_all.kind == "auto"
    assert fetch_all.no_process is False

    flagged = parser.parse_args(["fetch", FEED_URL, "--kind", "feed", "--no-process"])
    assert flagged.kind == "feed"
    assert flagged.no_process is True


def test_cli_fetch_delegates_and_prints_deterministic_json() -> None:
    api = FakeFetchAPI(make_cli_report())
    argv = ["fetch", FEED_URL]

    status, stdout, stderr = run_cli(argv, api)
    _, stdout_again, _ = run_cli(argv, api)

    assert status == 0
    assert stderr == ""
    assert api.calls == [("fetch_source", (FEED_URL, "auto", True))] * 2
    assert stdout_again == stdout
    payload = json.loads(stdout)
    assert payload["url"] == FEED_URL
    assert payload["fetched_at"] == "2026-09-01T12:00:00+00:00"
    assert payload["duplicate_keys"] == []
    (item,) = payload["items"]
    assert item["title"] == "Housing policy updated"
    assert item["material_id"].startswith("mat_")
    assert item["spool_path"] == "raw/spool/source-cli.md"
    assert item["raw_path"] == "raw/mat_cli.md"
    assert item["corpus_path"] == "corpus/Housing-policy-updated-mat_cli.md"
    assert item["pipeline"]["status"] == "completed"
    assert item["pipeline"]["started_at"] == "2026-09-01T12:00:00+00:00"


def test_cli_fetch_forwards_kind_and_no_process() -> None:
    api = FakeFetchAPI(make_cli_report())

    status, stdout, stderr = run_cli(
        ["fetch", FEED_URL, "--kind", "feed", "--no-process"], api
    )

    assert status == 0
    assert api.calls == [("fetch_source", (FEED_URL, "feed", False))]


def test_cli_fetch_failure_is_stderr_json_and_nonzero() -> None:
    api = FakeFetchAPI(
        SourceFetchError(
            FailureKind.BLOCKED,
            BLOCKED_URL,
            "host 'blocked.example.net' is not in the configured source whitelist",
        )
    )

    status, stdout, stderr = run_cli(["fetch", BLOCKED_URL], api)

    assert status == 1
    assert stdout == ""
    error = json.loads(stderr)["error"]
    assert error["type"] == "SourceFetchError"
    assert "whitelist" in error["message"]


def test_cli_fetch_all_accepts_json_file_and_comma_separated_urls(tmp_path) -> None:
    url_list = tmp_path / "urls.json"
    url_list.write_text(
        json.dumps([FEED_URL, MISSING_URL]), encoding="utf-8"
    )
    api = FakeFetchAPI(make_batch_report())

    status, stdout, stderr = run_cli(["fetch-all", str(url_list)], api)

    assert status == 0
    assert stderr == ""
    assert api.calls == [
        ("fetch_sources", ((FEED_URL, MISSING_URL), "auto", True))
    ]
    payload = json.loads(stdout)
    assert payload["reports"][0]["url"] == FEED_URL
    assert payload["failures"] == [
        {"detail": "HTTP 404", "kind": "http_status", "url": MISSING_URL}
    ]

    comma = FakeFetchAPI(make_batch_report())
    status, stdout, stderr = run_cli(
        ["fetch-all", f" {FEED_URL} , {MISSING_URL} "], comma
    )
    assert status == 0
    assert comma.calls == [("fetch_sources", ((FEED_URL, MISSING_URL), "auto", True))]


def test_cli_fetch_all_rejects_broken_url_sources(tmp_path) -> None:
    missing_file = tmp_path / "no-such.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    not_array = tmp_path / "object.json"
    not_array.write_text('{"urls": []}', encoding="utf-8")
    empty_array = tmp_path / "empty.json"
    empty_array.write_text("[]", encoding="utf-8")

    for source in (missing_file, invalid, not_array, empty_array):
        api = FakeFetchAPI(make_batch_report())
        status, stdout, stderr = run_cli(["fetch-all", str(source)], api)
        assert status == 1
        assert stdout == ""
        error = json.loads(stderr)["error"]
        assert error["type"] == "ValueError"
        assert api.calls == []

    api = FakeFetchAPI(make_batch_report())
    _, _, stderr = run_cli(["fetch-all", str(missing_file)], api)
    assert "URL list file not found" in json.loads(stderr)["error"]["message"]


def test_cli_default_runtime_fetch_stays_offline(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "cli-fetch-home"))
    url_list = tmp_path / "urls.json"
    url_list.write_text(json.dumps([FEED_URL]), encoding="utf-8")

    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(main(["fetch", FEED_URL], stdout=stdout, stderr=stderr))
    assert status == 1
    assert stdout.getvalue() == ""
    assert "source_service is required" in stderr.getvalue()

    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(
        main(["fetch-all", str(url_list)], stdout=stdout, stderr=stderr)
    )
    assert status == 1
    assert stdout.getvalue() == ""
    assert "source_service is required" in stderr.getvalue()
