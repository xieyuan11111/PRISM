"""Offline persistence tests for scholarly evidence levels.

These tests pin the ``doi``/``access_level``/``retrieval_level`` threading
from corpus frontmatter through the SQLite index (module 3) and back:
persistence across reopen, idempotent re-indexing, and automatic migration
of a database created by the previous schema.  Everything runs against
temporary directories; no network is touched.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from prism.api.fetching import spool_source_item
from prism.config import PathConfig
from prism.ingestion import IngestionService
from prism.pipeline import PipelineService
from prism.sources import FailureKind, SourceFetchError, SourceItem
from prism.store import EvidenceStore, SearchFilter

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)
DOI = "10.5555/example.123"
DOI_URL = "https://doi.org/10.5555/example.123"
PMID = "40212345"
PMCID = "PMC8880123"

# The schema as the previous release created it: neither ``documents`` nor
# ``document_fts`` carried the scholarly evidence columns.
_LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    source_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    type TEXT NOT NULL,
    case_tags TEXT NOT NULL DEFAULT '[]',
    original_format TEXT,
    ocr INTEGER NOT NULL DEFAULT 0,
    extracted_via TEXT,
    raw_path TEXT,
    url TEXT,
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    source_id UNINDEXED,
    title,
    source UNINDEXED,
    published_at UNINDEXED,
    fetched_at UNINDEXED,
    type UNINDEXED,
    case_tags UNINDEXED,
    original_format UNINDEXED,
    ocr UNINDEXED,
    extracted_via UNINDEXED,
    raw_path UNINDEXED,
    url UNINDEXED,
    path UNINDEXED,
    content,
    tokenize = 'unicode61'
);
"""


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        raw_dir=tmp_path / "raw",
        corpus_dir=tmp_path / "corpus",
    )


def write_paper(paths: PathConfig, name: str = "paper") -> Path:
    """Write one academic corpus document as ingestion renders it."""
    corpus = paths.corpus_dir
    corpus.mkdir(parents=True, exist_ok=True)
    path = corpus / f"{name}.md"
    path.write_text(
        "---\n"
        f'source_id: "mat_{name}"\n'
        f'title: "A scholarly work {name}"\n'
        'source: "academic"\n'
        'published_at: "2026-01-10T00:00:00+00:00"\n'
        'fetched_at: "2026-09-02T00:00:00+00:00"\n'
        'type: "academic"\n'
        "case_tags: []\n"
        'original_format: "md"\n'
        "ocr: false\n"
        'extracted_via: "direct"\n'
        f'raw_path: "raw/{name}.md"\n'
        f'url: "{DOI_URL}"\n'
        f'retrieval_level: "abstract_only"\n'
        f'access_level: "abstract_only"\n'
        f'doi: "{DOI}"\n'
        f'pmid: "{PMID}"\n'
        f'pmcid: "{PMCID}"\n'
        'authors: ["Ada Lovelace", "Grace Hopper"]\n'
        'container_title: "Journal of Evidence"\n'
        "---\n"
        "\n"
        "A public abstract about quantum policy evidence.\n",
        encoding="utf-8",
    )
    return path


def test_levels_and_doi_round_trip_across_reopen(tmp_path):
    paths = make_paths(tmp_path)
    doc = write_paper(paths)
    store = EvidenceStore(paths)
    outcome = store.index_file(doc)
    assert outcome.status == "indexed"
    assert outcome.entry.authors == ("Ada Lovelace", "Grace Hopper")
    assert outcome.entry.container_title == "Journal of Evidence"
    assert outcome.entry.pmid == PMID
    assert outcome.entry.pmcid == PMCID
    store.close()

    reopened = EvidenceStore(paths)
    entry = reopened.get(outcome.entry.source_id)
    assert entry is not None
    assert entry.retrieval_level == "abstract_only"
    assert entry.access_level == "abstract_only"
    assert entry.doi == DOI
    assert entry.authors == ("Ada Lovelace", "Grace Hopper")
    assert entry.container_title == "Journal of Evidence"
    assert entry.pmid == PMID
    assert entry.pmcid == PMCID

    hits = reopened.search(SearchFilter(query="quantum"))
    assert [hit.source_id for hit in hits] == [outcome.entry.source_id]
    assert hits[0].retrieval_level == "abstract_only"
    assert hits[0].access_level == "abstract_only"
    assert hits[0].doi == DOI
    assert hits[0].authors == ("Ada Lovelace", "Grace Hopper")
    assert hits[0].container_title == "Journal of Evidence"
    assert hits[0].pmid == PMID
    assert hits[0].pmcid == PMCID
    reopened.close()


def test_reindexing_scholarly_document_is_unchanged(tmp_path):
    paths = make_paths(tmp_path)
    doc = write_paper(paths)
    store = EvidenceStore(paths)
    try:
        assert store.index_file(doc).status == "indexed"
        assert store.index_file(doc).status == "unchanged"
    finally:
        store.close()


def test_no_doi_pubmed_identifiers_round_trip_end_to_end(tmp_path):
    paths = make_paths(tmp_path)
    item = SourceItem(
        title="A PubMed-only work",
        source="academic",
        fetched_at=NOW,
        published_at=datetime(2024, 3, 4, tzinfo=timezone.utc),
        link=f"https://pmc.ncbi.nlm.nih.gov/articles/{PMCID}/",
        summary="A public abstract without a DOI.",
        type="academic",
        retrieval_level="abstract_only",
        access_level="abstract_only",
        pmid=PMID,
        pmcid=PMCID,
        authors=["Ada Lovelace"],
    )
    spool = spool_source_item(item, paths.raw_dir / "spool")
    ingested = IngestionService(paths).ingest(spool, item.to_ingestion_metadata())
    assert ingested.material.doi is None
    assert ingested.material.pmid == PMID
    assert ingested.material.pmcid == PMCID
    assert ingested.material.authors == ("Ada Lovelace",)
    corpus = ingested.corpus_path.read_text(encoding="utf-8")
    assert f'pmid: "{PMID}"' in corpus
    assert f'pmcid: "{PMCID}"' in corpus

    store = EvidenceStore(paths)
    try:
        outcome = store.index_file(ingested.corpus_path)
        assert outcome.entry.doi is None
        assert outcome.entry.pmid == PMID
        assert outcome.entry.pmcid == PMCID
        hit = store.search(SearchFilter(query="abstract"))[0]
        assert hit.doi is None
        assert hit.pmid == PMID
        assert hit.pmcid == PMCID
    finally:
        store.close()


def test_legacy_database_is_migrated_and_keeps_legacy_rows(tmp_path):
    paths = make_paths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    seed = sqlite3.connect(paths.data_dir / "index.db")
    seed.executescript(_LEGACY_SCHEMA)
    seed.execute(
        "INSERT INTO documents (source_id, title, source, published_at,"
        " fetched_at, type, case_tags, original_format, ocr, extracted_via,"
        " raw_path, url, path, content, content_hash, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "mat_legacy",
            "Legacy policy",
            "example.gov",
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
            "policy",
            "[]",
            "md",
            0,
            "direct",
            None,
            None,
            "corpus/legacy.md",
            "Legacy housing policy quantum body text.",
            "hash-legacy",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    rowid = seed.execute(
        "SELECT rowid FROM documents WHERE source_id = 'mat_legacy'"
    ).fetchone()[0]
    seed.execute(
        "INSERT INTO document_fts (rowid, source_id, title, source,"
        " published_at, fetched_at, type, case_tags, original_format, ocr,"
        " extracted_via, raw_path, url, path, content)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rowid,
            "mat_legacy",
            "Legacy policy",
            "example.gov",
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
            "policy",
            "[]",
            "md",
            0,
            "direct",
            None,
            None,
            "corpus/legacy.md",
            "Legacy housing policy quantum body text.",
        ),
    )
    seed.commit()
    seed.close()

    store = EvidenceStore(paths)
    try:
        store.initialize()

        legacy = store.get("mat_legacy")
        assert legacy is not None
        assert legacy.doi is None
        assert legacy.access_level is None
        assert legacy.authors == ()
        assert legacy.container_title is None
        assert legacy.pmid is None
        assert legacy.pmcid is None

        assert [hit.source_id for hit in store.search(SearchFilter(query="legacy"))] == [
            "mat_legacy"
        ]
        legacy_hit = store.search(SearchFilter(query="legacy"))[0]
        assert legacy_hit.authors == ()
        assert legacy_hit.container_title is None
        assert legacy_hit.pmid is None
        assert legacy_hit.pmcid is None

        outcome = store.index_file(write_paper(paths))
        assert outcome.status == "indexed"
        stored = store.get(outcome.entry.source_id)
        assert stored.doi == DOI
        assert stored.authors == ("Ada Lovelace", "Grace Hopper")
        assert stored.container_title == "Journal of Evidence"
        assert stored.pmid == PMID
        assert stored.pmcid == PMCID
    finally:
        store.close()


class FakeSource:
    async def fetch(self, url, *, kind="auto"):
        raise SourceFetchError(FailureKind.HTTP_STATUS, url, "HTTP 403")


class FakeScholarly:
    async def fetch(self, value):
        return SourceItem(
            title="A scholarly work",
            source="academic",
            fetched_at=NOW,
            link=DOI_URL,
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            summary="A public abstract about quantum policy evidence.",
            content=None,
            type="academic",
            access_level="abstract_only",
            retrieval_level="abstract_only",
            doi=DOI,
            authors=("Ada Lovelace",),
            container_title="Journal of Evidence",
        )


class MetadataOnlyFakeScholarly:
    async def fetch(self, value):
        return SourceItem(
            title="A metadata-only work",
            source="academic",
            fetched_at=NOW,
            link=DOI_URL,
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            summary=None,
            content=None,
            type="academic",
            access_level="metadata_only",
            retrieval_level="metadata_only",
            doi=DOI,
            authors=("Ada Lovelace", "Grace Hopper"),
            container_title="Journal of Evidence",
        )


class FakeGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("not used")

    async def add_case(self, case, **kwargs):
        raise AssertionError("not used")


class FakeEvents:
    async def publish(self, event):
        pass


def test_scholarly_item_flows_to_index_entry_offline(tmp_path):
    """SourceItem -> ingestion -> corpus frontmatter -> index, all offline."""
    from prism.api import PrismAPI

    paths = make_paths(tmp_path)
    ingestion = IngestionService(paths)
    store = EvidenceStore(paths)
    store.initialize()
    api = PrismAPI(
        ingestion,
        store,
        FakeGraph(),
        FakeEvents(),
        source_service=FakeSource(),
        source_raw_dir=tmp_path / "raw",
        scholarly_metadata_client=FakeScholarly(),
    )

    report = asyncio.run(api.fetch_source(DOI_URL, process=False))

    corpus_text = report.items[0].corpus_path.read_text(encoding="utf-8")
    assert 'access_level: "abstract_only"' in corpus_text
    assert 'retrieval_level: "abstract_only"' in corpus_text
    assert f'doi: "{DOI}"' in corpus_text
    assert '"Ada Lovelace"' in corpus_text
    assert 'container_title: "Journal of Evidence"' in corpus_text

    outcome = store.index_file(report.items[0].corpus_path)
    assert outcome.entry.access_level == "abstract_only"
    assert outcome.entry.doi == DOI
    assert outcome.entry.authors == ("Ada Lovelace",)
    assert outcome.entry.container_title == "Journal of Evidence"
    store.close()

    reopened = EvidenceStore(paths)
    try:
        entry = reopened.get(outcome.entry.source_id)
        assert entry.access_level == "abstract_only"
        assert entry.retrieval_level == "abstract_only"
        assert entry.doi == DOI
        assert entry.authors == ("Ada Lovelace",)
        assert entry.container_title == "Journal of Evidence"
    finally:
        reopened.close()


def test_metadata_only_item_keeps_bibliography_through_index_and_reopen(tmp_path):
    """A metadata-only SourceItem keeps authors/container_title/doi end to end."""
    from prism.api import PrismAPI

    paths = make_paths(tmp_path)
    ingestion = IngestionService(paths)
    store = EvidenceStore(paths)
    store.initialize()
    api = PrismAPI(
        ingestion,
        store,
        FakeGraph(),
        FakeEvents(),
        source_service=FakeSource(),
        source_raw_dir=tmp_path / "raw",
        scholarly_metadata_client=MetadataOnlyFakeScholarly(),
    )

    report = asyncio.run(api.fetch_source(DOI_URL, process=False))

    # The spool body is an honest placeholder, never full text.
    spool_text = report.items[0].spool_path.read_text(encoding="utf-8")
    assert "Full text was not available" in spool_text
    assert "Ada Lovelace, Grace Hopper" in spool_text
    assert "Journal of Evidence" in spool_text

    # The bibliographic identity lives in the corpus frontmatter.
    corpus_text = report.items[0].corpus_path.read_text(encoding="utf-8")
    assert 'access_level: "metadata_only"' in corpus_text
    assert 'retrieval_level: "metadata_only"' in corpus_text
    assert f'doi: "{DOI}"' in corpus_text
    assert '"Ada Lovelace", "Grace Hopper"' in corpus_text
    assert 'container_title: "Journal of Evidence"' in corpus_text

    outcome = store.index_file(report.items[0].corpus_path)
    assert outcome.entry.access_level == "metadata_only"
    assert outcome.entry.doi == DOI
    assert outcome.entry.authors == ("Ada Lovelace", "Grace Hopper")
    assert outcome.entry.container_title == "Journal of Evidence"
    store.close()

    reopened = EvidenceStore(paths)
    try:
        entry = reopened.get(outcome.entry.source_id)
        assert entry.access_level == "metadata_only"
        assert entry.retrieval_level == "metadata_only"
        assert entry.doi == DOI
        assert entry.authors == ("Ada Lovelace", "Grace Hopper")
        assert entry.container_title == "Journal of Evidence"
    finally:
        reopened.close()


class RecordingExtractor:
    """Extraction spy: non-fulltext materials must never reach it."""

    def __init__(self):
        self.calls = []

    async def extract(self, material):
        self.calls.append(material)
        from prism.extraction import ExtractionResult
        return ExtractionResult()


class ForbiddenGraph:
    """Graph stub that fails loudly if a non-fulltext material reaches it."""

    async def timeline(self, case_id, as_of):
        raise AssertionError("timeline is not part of the fetch path")

    async def add_case(self, case, **bundle):
        raise AssertionError("graph writing must be skipped for non-fulltext materials")


def test_fetch_source_default_process_skips_extraction_for_abstract_only(tmp_path):
    """process=True (default) indexes but never LLM-extracts an abstract."""
    from prism.api import PrismAPI

    paths = make_paths(tmp_path)
    ingestion = IngestionService(paths)
    store = EvidenceStore(paths)
    store.initialize()
    extractor = RecordingExtractor()
    graph = ForbiddenGraph()
    pipeline = PipelineService(
        indexer=store, extraction_service=extractor, graph_service=graph
    )
    api = PrismAPI(
        ingestion,
        store,
        graph,
        FakeEvents(),
        source_service=FakeSource(),
        pipeline_service=pipeline,
        source_raw_dir=tmp_path / "raw",
        scholarly_metadata_client=FakeScholarly(),
    )

    report = asyncio.run(api.fetch_source(DOI_URL))  # process defaults to True

    (item,) = report.items
    assert item.access_level == "abstract_only"
    run = item.pipeline
    assert run is not None and run.status == "completed"
    assert [stage.name for stage in run.stages] == ["index", "extract", "graph"]
    assert [stage.status for stage in run.stages] == [
        "indexed",
        "skipped",
        "skipped",
    ]
    assert "abstract_only" in run.stages[1].detail
    assert extractor.calls == []  # the abstract was never treated as a body

    entry = store.get(item.material_id)
    assert entry is not None
    assert entry.access_level == "abstract_only"
    assert entry.doi == DOI
    assert entry.authors == ("Ada Lovelace",)
    assert entry.container_title == "Journal of Evidence"
