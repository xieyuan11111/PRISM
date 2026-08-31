"""Tests for the PRISM SQLite/FTS5 text evidence index (module 3).

All tests are offline: they use temporary SQLite databases and temporary
Markdown corpus files under ``tmp_path`` (never the network, never an LLM).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prism.config import PathConfig
from prism.store import BatchResult, EvidenceStore, SearchFilter, SearchHit

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        raw_dir=Path("raw"),
        corpus_dir=Path("corpus"),
    )


def corpus_doc(name: str, body: str | None = None, _raw: str | None = None, **overrides) -> str:
    """Render a standard corpus Markdown document (as ingestion writes it)."""
    if _raw is not None:
        return _raw
    fields = {
        "source_id": f"mat_{name}",
        "title": f"Title {name}",
        "source": "example.gov",
        "published_at": "2026-08-31T12:00:00+00:00",
        "fetched_at": "2026-08-31T12:00:00+00:00",
        "type": "policy",
        "case_tags": ["housing"],
        "original_format": "md",
        "ocr": False,
        "extracted_via": "direct",
        "raw_path": f"raw/{name}.md",
        "url": f"https://example.gov/{name}",
    }
    fields.update(overrides)
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            rendered = json.dumps(list(value))
        elif isinstance(value, str):
            rendered = json.dumps(value)
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    lines.append("")
    lines.append(body if body is not None else f"# {name}\n\nBody text about housing policy.")
    return "\n".join(lines) + "\n"


def write_corpus(store: EvidenceStore, name: str, index: bool = True, **overrides) -> Path:
    store.paths.corpus_dir.mkdir(parents=True, exist_ok=True)
    path = store.paths.corpus_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(corpus_doc(Path(name).stem, **overrides), encoding="utf-8")
    if index:
        store.index_file(path)
    return path


@pytest.fixture
def paths(tmp_path):
    return make_paths(tmp_path)


@pytest.fixture
def store(paths):
    instance = EvidenceStore(paths)
    instance.initialize()
    yield instance
    instance.close()


# ---------------------------------------------------------------------------
# Database location and schema initialization
# ---------------------------------------------------------------------------


def test_db_lives_under_data_dir_and_schema_is_initialized(paths, tmp_path):
    store = EvidenceStore(paths)
    assert store.db_path == (tmp_path / "data" / "index.db").resolve()

    store.initialize()
    store.initialize()  # idempotent

    assert store.db_path.is_file()
    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
    assert "documents" in tables
    assert "document_fts" in tables
    store.close()


def test_schema_has_fts5_and_metadata_indexes(paths):
    store = EvidenceStore(paths)
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'document_fts'"
        ).fetchone()[0]
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'documents'"
            )
        }
    assert "USING fts5" in fts_sql
    assert {"documents_source_idx", "documents_type_idx", "documents_published_at_idx"} <= indexes
    store.close()


# ---------------------------------------------------------------------------
# Indexing corpus Markdown files
# ---------------------------------------------------------------------------


def test_index_file_parses_frontmatter_and_indexes_all_fields(store):
    path = write_corpus(store, "nested/dir/doc.md", index=False, ocr=True,
                        extracted_via="fake-ocr")

    outcome = store.index_file(path)

    assert outcome.status == "indexed"
    entry = outcome.entry
    assert entry.source_id == "mat_doc"
    assert entry.title == "Title doc"
    assert entry.source == "example.gov"
    assert entry.published_at == NOW
    assert entry.fetched_at == NOW
    assert entry.type == "policy"
    assert entry.case_tags == ("housing",)
    assert entry.original_format == "md"
    assert entry.ocr is True
    assert entry.extracted_via == "fake-ocr"
    assert entry.raw_path == "raw/doc.md"
    assert entry.url == "https://example.gov/doc"
    assert entry.path == "corpus/nested/dir/doc.md"
    assert entry.content == "# doc\n\nBody text about housing policy."
    assert len(entry.content_hash) == 64

    # The record is readable back through the store.
    stored = store.get("mat_doc")
    assert stored == entry


def test_missing_source_id_is_derived_deterministically(store):
    body = "Same body about housing policy."
    first = write_corpus(store, "a.md", index=False, source_id=None, body=body)
    second = write_corpus(store, "sub/b.md", index=False, source_id=None, body=body)

    o1 = store.index_file(first)
    o2 = store.index_file(first)
    o3 = store.index_file(second)

    assert o1.status == "indexed"
    assert o2.status == "unchanged"  # identical re-index is a no-op
    assert o1.entry.source_id == o3.entry.source_id
    assert o1.entry.source_id.startswith("mat_")
    assert o3.status == "updated"  # same content-derived id, new title/path
    assert len(store.search()) == 1  # no duplicate record


def test_index_file_missing_file_raises(store):
    with pytest.raises(FileNotFoundError):
        store.index_file(store.paths.corpus_dir / "nope.md")


def test_index_directory_scans_corpus_by_default_and_reports_errors(store):
    write_corpus(store, "good.md", index=False)
    write_corpus(store, "no-fm.md", index=False, _raw="# no frontmatter\n\nsome body")
    write_corpus(
        store,
        "empty.md",
        index=False,
        _raw="---\nsource_id: mat_empty\nsource: example.gov\n"
        "published_at: 2026-08-31T12:00:00+00:00\n---\n\n   \n",
    )

    result = store.index_directory()

    assert isinstance(result, BatchResult)
    assert result.total == 3
    assert result.indexed == 1
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.failed == 2
    assert len(result.errors) == 2
    error_names = {Path(path).name for path, _ in result.errors}
    assert error_names == {"no-fm.md", "empty.md"}
    assert store.get("mat_good") is not None
    assert store.get("mat_empty") is None  # invalid file left no record behind

    # Re-scanning is stable: nothing new, nothing duplicated.
    again = store.index_directory()
    assert again.indexed == 0
    assert again.updated == 0
    assert again.unchanged == 1
    assert again.failed == 2


def test_index_directory_explicit_path_and_missing_directory(store, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.md").write_text(corpus_doc("x"), encoding="utf-8")

    result = store.index_directory(elsewhere)
    assert result.total == 1
    assert result.indexed == 1

    with pytest.raises(FileNotFoundError):
        store.index_directory(tmp_path / "does-not-exist")


# ---------------------------------------------------------------------------
# Full-text search and filters
# ---------------------------------------------------------------------------


def test_naive_datetime_strings_are_rejected(store):
    write_corpus(
        store,
        "naive.md",
        index=False,
        published_at="2026-08-31T12:00:00",
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        store.index_file(store.paths.corpus_dir / "naive.md")


def test_search_is_timezone_correct_for_equivalent_instants(store):
    write_corpus(
        store,
        "offset.md",
        published_at="2026-08-31T20:00:00+08:00",
        body="# offset\n\nA policy about housing.",
    )

    hits = store.search(
        SearchFilter(
            query="housing",
            published_after=datetime(2026, 8, 31, 11, 59, tzinfo=timezone.utc),
            published_before=datetime(2026, 8, 31, 12, 1, tzinfo=timezone.utc),
        )
    )

    assert [hit.source_id for hit in hits] == ["mat_offset"]


def test_search_treats_fts_syntax_as_literal_text(store):
    write_corpus(
        store,
        "literal.md",
        body="# literal\n\nThe policy uses the phrase C++ and OR literally.",
    )

    hits = store.search(SearchFilter(query='C++ OR "literal"'))

    assert [hit.source_id for hit in hits] == ["mat_literal"]


def test_search_matches_cjk_adjacent_to_ascii_and_digits(store):
    write_corpus(
        store,
        "mixed.md",
        body="# mixed\n\n2026年政策A1 applies to housing.",
    )

    assert [hit.source_id for hit in store.search(SearchFilter(query="政策A1"))] == ["mat_mixed"]


def test_search_returns_hits_with_snippet_and_metadata(store):
    write_corpus(store, "a.md")
    write_corpus(store, "b.md", title="Another title", source="news.example.org",
                 type="news", case_tags=["housing", "economy"], url=None)

    hits = store.search(SearchFilter(query="housing"))

    assert len(hits) == 2
    hit = next(h for h in hits if h.source_id == "mat_a")
    assert isinstance(hit, SearchHit)
    assert hit.source_id == "mat_a"
    assert hit.title == "Title a"
    assert hit.path == "corpus/a.md"
    assert hit.source == "example.gov"
    assert hit.type == "policy"
    assert hit.published_at == NOW
    assert hit.case_tags == ("housing",)
    assert hit.url == "https://example.gov/a"
    assert "housing" in hit.snippet


def test_search_filters_by_case_tag(store):
    write_corpus(store, "a.md", case_tags=["housing"])
    write_corpus(store, "b.md", case_tags=["education"])

    hits = store.search(SearchFilter(case_tag="housing"))

    assert [h.source_id for h in hits] == ["mat_a"]


def test_search_filters_by_source_and_type(store):
    write_corpus(store, "a.md", source="example.gov", type="policy")
    write_corpus(store, "b.md", source="news.example.org", type="news")

    assert [h.source_id for h in store.search(SearchFilter(source="example.gov"))] == ["mat_a"]
    assert [h.source_id for h in store.search(SearchFilter(type="news"))] == ["mat_b"]


def test_search_filters_by_published_time_range(store):
    write_corpus(store, "a.md", published_at="2026-08-01T00:00:00+00:00")
    write_corpus(store, "b.md", published_at="2026-08-15T00:00:00+00:00")
    write_corpus(store, "c.md", published_at="2026-08-31T00:00:00+00:00")

    after = store.search(
        SearchFilter(published_after=datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc))
    )
    assert [h.source_id for h in after] == ["mat_c", "mat_b"]

    before = store.search(
        SearchFilter(published_before=datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc))
    )
    assert [h.source_id for h in before] == ["mat_b", "mat_a"]


def test_search_combines_query_and_filters(store):
    write_corpus(store, "a.md", source="example.gov", type="policy", case_tags=["housing"])
    write_corpus(store, "b.md", source="news.example.org", type="policy", case_tags=["housing"])
    write_corpus(store, "c.md", source="example.gov", type="news", case_tags=["housing"])

    hits = store.search(
        SearchFilter(
            query="housing",
            case_tag="housing",
            source="example.gov",
            type="policy",
            published_after=datetime(2026, 8, 1, tzinfo=timezone.utc),
            published_before=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
    )

    assert [h.source_id for h in hits] == ["mat_a"]


def test_search_supports_chinese_full_text(store):
    write_corpus(store, "cn.md", body="房地产政策出台后市场反应积极。", case_tags=["housing"])

    hits = store.search(SearchFilter(query="政策"))

    assert len(hits) == 1
    assert hits[0].source_id == "mat_cn"
    assert "政策" in hits[0].snippet


def test_search_without_query_returns_everything(store):
    write_corpus(store, "a.md")
    write_corpus(store, "b.md")

    assert len(store.search()) == 2


def test_search_ordering_is_stable_without_query(store):
    write_corpus(store, "a.md", published_at="2026-08-30T00:00:00+00:00")
    write_corpus(store, "b.md", published_at="2026-08-31T00:00:00+00:00")
    write_corpus(store, "c.md", published_at="2026-08-31T00:00:00+00:00")
    write_corpus(store, "d.md", published_at="2026-08-29T00:00:00+00:00")

    first = [h.source_id for h in store.search()]
    second = [h.source_id for h in store.search()]

    assert first == ["mat_b", "mat_c", "mat_a", "mat_d"]  # published desc, then source_id
    assert first == second


def test_search_rank_ordering_is_stable(store):
    body = "identical body about housing policy repeated here"
    write_corpus(store, "a.md", title="Same title", body=body)
    write_corpus(store, "b.md", title="Same title", body=body)

    first = [h.source_id for h in store.search(SearchFilter(query="housing"))]
    second = [h.source_id for h in store.search(SearchFilter(query="housing"))]

    assert first == ["mat_a", "mat_b"]  # equal rank -> deterministic source_id tiebreak
    assert first == second


def test_search_limit_and_offset(store):
    write_corpus(store, "a.md", published_at="2026-08-01T00:00:00+00:00")
    write_corpus(store, "b.md", published_at="2026-08-15T00:00:00+00:00")
    write_corpus(store, "c.md", published_at="2026-08-31T00:00:00+00:00")

    page1 = store.search(limit=2)
    page2 = store.search(limit=2, offset=2)

    assert [h.source_id for h in page1] == ["mat_c", "mat_b"]
    assert [h.source_id for h in page2] == ["mat_a"]


def test_search_argument_validation(store):
    with pytest.raises(ValueError, match="limit"):
        store.search(limit=0)
    with pytest.raises(ValueError, match="offset"):
        store.search(offset=-1)
    with pytest.raises(ValueError, match="query"):
        store.search(SearchFilter(query="   "))
    with pytest.raises(ValueError, match="case_tag"):
        store.search(SearchFilter(case_tag=""))
    with pytest.raises(ValueError, match="published_after"):
        SearchFilter(published_after=datetime(2026, 8, 31, 12, 0))  # naive
    with pytest.raises(ValueError, match="published_after"):
        SearchFilter(published_after=NOW, published_before=NOW - timedelta(days=1))


def test_invalid_full_text_query_raises_clear_error(store):
    write_corpus(store, "a.md")

    with pytest.raises(ValueError, match="full-text"):
        store.search(SearchFilter(query='"'))


def test_delete_removes_record_and_fts_entry(store):
    write_corpus(store, "a.md")
    write_corpus(store, "b.md")

    assert store.delete("mat_a") is True
    assert store.get("mat_a") is None
    assert [h.source_id for h in store.search(SearchFilter(query="housing"))] == ["mat_b"]
    assert store.delete("mat_a") is False


# ---------------------------------------------------------------------------
# Idempotent upserts: updates without duplicates
# ---------------------------------------------------------------------------


def test_reindexing_identical_file_is_a_no_op(store):
    path = write_corpus(store, "a.md", index=False)

    first = store.index_file(path)
    second = store.index_file(path)

    assert first.status == "indexed"
    assert second.status == "unchanged"
    assert len(store.search()) == 1


def test_content_change_updates_the_record(store):
    path = write_corpus(store, "a.md", index=False)

    store.index_file(path)
    path.write_text(corpus_doc("a", body="Revised body about urban renewal."), encoding="utf-8")
    outcome = store.index_file(path)

    assert outcome.status == "updated"
    assert store.get("mat_a").content == "Revised body about urban renewal."
    assert len(store.search()) == 1
    assert store.search(SearchFilter(query="housing")) == []  # old text gone
    assert [h.source_id for h in store.search(SearchFilter(query="renewal"))] == ["mat_a"]


def test_metadata_change_updates_the_record(store):
    path = write_corpus(store, "a.md", index=False, title="Original title")

    store.index_file(path)
    path.write_text(corpus_doc("a", title="Renamed title"), encoding="utf-8")
    outcome = store.index_file(path)

    assert outcome.status == "updated"
    assert store.get("mat_a").title == "Renamed title"
    assert len(store.search()) == 1
    assert [h.source_id for h in store.search(SearchFilter(query="renamed"))] == ["mat_a"]


# ---------------------------------------------------------------------------
# Error isolation: bad files never pollute existing records
# ---------------------------------------------------------------------------


def test_missing_frontmatter_raises_clear_error(store):
    path = write_corpus(store, "bad.md", index=False, _raw="# No frontmatter\n\nbody text")

    with pytest.raises(ValueError, match="frontmatter"):
        store.index_file(path)


def test_corrupt_frontmatter_raises_clear_error(store):
    path = write_corpus(
        store,
        "bad.md",
        index=False,
        _raw="---\nsource_id: mat_bad\nsource: example.gov\n--- not closed\nbody",
    )

    with pytest.raises(ValueError, match="frontmatter"):
        store.index_file(path)


def test_missing_required_metadata_raises_clear_errors(store):
    no_source = write_corpus(
        store,
        "no-source.md",
        index=False,
        _raw="---\nsource_id: mat_ns\npublished_at: 2026-08-31T12:00:00+00:00\n---\nbody",
    )
    with pytest.raises(ValueError, match="source"):
        store.index_file(no_source)

    no_date = write_corpus(
        store, "no-date.md", index=False,
        _raw="---\nsource_id: mat_nd\nsource: example.gov\n---\nbody",
    )
    with pytest.raises(ValueError, match="published_at"):
        store.index_file(no_date)

    bad_date = write_corpus(
        store,
        "bad-date.md",
        index=False,
        _raw="---\nsource_id: mat_bd\nsource: example.gov\n"
        "published_at: not-a-date\n---\nbody",
    )
    with pytest.raises(ValueError, match="published_at"):
        store.index_file(bad_date)


def test_empty_body_raises_clear_error(store):
    path = write_corpus(
        store,
        "blank.md",
        index=False,
        _raw="---\nsource_id: mat_blank\nsource: example.gov\n"
        "published_at: 2026-08-31T12:00:00+00:00\n---\n\n   \n",
    )

    with pytest.raises(ValueError, match="empty"):
        store.index_file(path)


def test_failed_reindex_does_not_pollute_existing_record(store):
    path = write_corpus(store, "doc.md", index=False)
    store.index_file(path)

    path.write_text(
        "---\nsource_id: mat_doc\nsource: example.gov\n"
        "published_at: not-a-date\n---\nbody",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="published_at"):
        store.index_file(path)

    stored = store.get("mat_doc")
    assert stored is not None
    assert stored.content == "# doc\n\nBody text about housing policy."
    assert [h.source_id for h in store.search(SearchFilter(query="housing"))] == ["mat_doc"]


def test_failed_index_leaves_no_partial_row(store):
    path = write_corpus(
        store,
        "bad.md",
        index=False,
        _raw="---\nsource_id: mat_bad\nsource: example.gov\n--- not closed\nbody",
    )

    with pytest.raises(ValueError, match="frontmatter"):
        store.index_file(path)

    assert store.get("mat_bad") is None
    assert store.search(SearchFilter(query="body")) == []


# ---------------------------------------------------------------------------
# Portable project-relative paths
# ---------------------------------------------------------------------------


def test_relative_paths_use_prism_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path))
    store = EvidenceStore(PathConfig())
    store.initialize()

    corpus = store.paths.corpus_dir
    corpus.mkdir(parents=True, exist_ok=True)
    doc = corpus / "a.md"
    doc.write_text(corpus_doc("a"), encoding="utf-8")

    outcome = store.index_file(doc)
    assert outcome.status == "indexed"
    assert outcome.entry.path == "corpus/a.md"
    assert store.db_path == (tmp_path / "data" / "index.db").resolve()
    assert store.db_path.is_file()
    store.close()


def test_file_outside_project_root_is_rejected(store, tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text(corpus_doc("x"), encoding="utf-8")

    with pytest.raises(ValueError, match="project root"):
        store.index_file(outside)


def test_store_context_manager_closes_connection(paths):
    with EvidenceStore(paths) as store:
        store.initialize()
        assert store.get("anything") is None
