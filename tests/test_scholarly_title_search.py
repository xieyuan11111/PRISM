"""Strict bibliographic title search over Crossref/OpenAlex (offline HTTP)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from prism.sources import (
    CrossrefClient,
    FailureKind,
    HttpResponse,
    OpenAlexClient,
    ScholarlyMetadataClient,
    SourceFetchError,
    extract_doi,
)

UTC = timezone.utc
RETRIEVED = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
TITLE = (
    "Machine learning approaches for quantitative forecasting of public "
    "health evidence synthesis across large-scale multilingual corpora"
)
DOI = "10.1000/title.42"
CROSSREF_SEARCH_URL = (
    "https://api.crossref.org/works?query.bibliographic="
    + quote(TITLE, safe="")
    + "&rows=5"
)
OPENALEX_SEARCH_URL = (
    "https://api.openalex.org/works?filter="
    + quote(f"title.search:{TITLE}", safe="")
    + "&per-page=5"
)


class FakeGetter:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append(url)
        result = self.routes[url]
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


def response(url: str, payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(url=url, status=status, body=json.dumps(payload))


def run(coro):
    return asyncio.run(coro)


def crossref_item(title: str, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "title": [title],
        "DOI": DOI,
        "container-title": ["Journal of Public Evidence"],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "published": {"date-parts": [[2024, 3, 4]]},
    }
    item.update(overrides)
    return item


def crossref_search(items: list[object]) -> dict[str, object]:
    return {"status": "ok", "message": {"items": items, "total-results": len(items)}}


def openalex_work(title: str, **overrides: object) -> dict[str, object]:
    work: dict[str, object] = {
        "display_name": title,
        "publication_date": "2024-03-04",
        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
        "ids": {"doi": "https://doi.org/" + DOI},
        "abstract_inverted_index": {"A": [0], "abstract": [1]},
    }
    work.update(overrides)
    return work


def client_with(crossref_routes: dict[str, object], openalex_routes: dict[str, object]):
    crossref = FakeGetter(crossref_routes)
    openalex = FakeGetter(openalex_routes)
    return (
        ScholarlyMetadataClient(
            CrossrefClient(crossref),
            OpenAlexClient(openalex),
            clock=lambda: RETRIEVED,
        ),
        crossref,
        openalex,
    )


def test_crossref_search_accepts_normalized_exact_title_match():
    item = crossref_item("Machine   LEARNING approaches for quantitative forecasting "
                         "of public health evidence synthesis across large-scale multilingual corpora!")
    getter = FakeGetter({CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([item]))})
    record = run(CrossrefClient(getter).search(TITLE, retrieved_at=RETRIEVED))
    assert record is not None
    assert record.doi == DOI
    assert record.title.startswith("Machine LEARNING")
    assert record.container_title == "Journal of Public Evidence"
    assert getter.calls == [CROSSREF_SEARCH_URL]


def test_crossref_search_skips_non_matching_items_until_the_verified_match():
    other = crossref_item("A retrospective cohort study of unrelated outcomes")
    match = crossref_item(TITLE)
    getter = FakeGetter({
        CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([other, match])),
    })
    record = run(CrossrefClient(getter).search(TITLE, retrieved_at=RETRIEVED))
    assert record is not None
    assert record.doi == DOI


def test_crossref_search_returns_none_when_only_similar_titles_come_back():
    near_miss = crossref_item(
        "Machine learning approaches for qualitative review of public health "
        "evidence synthesis across large-scale multilingual corpora"
    )
    getter = FakeGetter({
        CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([near_miss])),
    })
    assert run(CrossrefClient(getter).search(TITLE, retrieved_at=RETRIEVED)) is None


def test_search_accepts_typo_level_differences_on_long_titles():
    typo = TITLE.replace("forecasting", "firecasting")
    assert typo != TITLE
    getter = FakeGetter({CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([crossref_item(typo)]))})
    record = run(CrossrefClient(getter).search(TITLE, retrieved_at=RETRIEVED))
    assert record is not None  # a one-character typo is not a different work


def test_search_item_without_any_identifier_is_not_accepted():
    no_doi = crossref_item(TITLE)
    no_doi.pop("DOI")
    getter = FakeGetter({CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([no_doi]))})
    assert run(CrossrefClient(getter).search(TITLE, retrieved_at=RETRIEVED)) is None


def test_crossref_search_response_without_items_is_classified_as_parse():
    getter = FakeGetter({CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, {"status": "ok", "message": {}})})
    with pytest.raises(SourceFetchError) as caught:
        run(CrossrefClient(getter).search(TITLE, retrieved_at=RETRIEVED))
    assert caught.value.kind is FailureKind.PARSE


def test_openalex_search_matches_on_display_name_and_needs_identifier():
    work = openalex_work(TITLE)
    getter = FakeGetter({OPENALEX_SEARCH_URL: response(OPENALEX_SEARCH_URL, {"results": [work]})})
    record = run(OpenAlexClient(getter).search(TITLE, retrieved_at=RETRIEVED))
    assert record is not None
    assert record.doi == DOI
    assert record.abstract == "A abstract"


def test_openalex_search_match_without_doi_keeps_pubmed_identifiers():
    work = openalex_work(
        TITLE,
        ids={"pmid": "https://pubmed.ncbi.nlm.nih.gov/40212345/",
             "pmcid": "https://pmc.ncbi.nlm.nih.gov/articles/PMC8880123/"},
    )
    getter = FakeGetter({OPENALEX_SEARCH_URL: response(OPENALEX_SEARCH_URL, {"results": [work]})})
    record = run(OpenAlexClient(getter).search(TITLE, retrieved_at=RETRIEVED))
    assert record is not None
    assert record.doi is None
    assert record.pmid == "40212345"
    assert record.pmcid == "PMC8880123"
    item = record.to_source_item()
    assert item.doi is None
    assert item.to_ingestion_metadata()["pmid"] == "40212345"


def test_fetch_by_title_uses_crossref_then_openalex_once_each():
    openalex_work_payload = openalex_work(TITLE)
    client, crossref, openalex = client_with(
        {CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([]))},
        {OPENALEX_SEARCH_URL: response(OPENALEX_SEARCH_URL, {"results": [openalex_work_payload]})},
    )
    item = run(client.fetch_by_title(TITLE, link="https://www.nature.com/articles/xyz"))
    assert item.title == TITLE
    assert item.summary == "A abstract"
    assert item.content is None
    assert item.access_level == "abstract_only"
    assert item.doi == DOI
    assert crossref.calls == [CROSSREF_SEARCH_URL]
    assert openalex.calls == [OPENALEX_SEARCH_URL]


def test_fetch_by_title_refuses_to_guess_when_nothing_matches():
    client, crossref, openalex = client_with(
        {CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([]))},
        {OPENALEX_SEARCH_URL: response(OPENALEX_SEARCH_URL, {"results": []})},
    )
    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch_by_title(TITLE))
    assert caught.value.kind is FailureKind.PARSE
    assert "no confident bibliographic match" in caught.value.detail
    assert crossref.calls == [CROSSREF_SEARCH_URL]
    assert openalex.calls == [OPENALEX_SEARCH_URL]


def test_fetch_by_title_preserves_the_crossref_failure_classification():
    client, crossref, openalex = client_with(
        {CROSSREF_SEARCH_URL: SourceFetchError(FailureKind.HTTP_STATUS, CROSSREF_SEARCH_URL, "HTTP 429")},
        {OPENALEX_SEARCH_URL: SourceFetchError(FailureKind.TIMEOUT, OPENALEX_SEARCH_URL, "timed out")},
    )
    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch_by_title(TITLE))
    assert caught.value.kind is FailureKind.HTTP_STATUS
    assert caught.value.url == CROSSREF_SEARCH_URL
    assert crossref.calls == [CROSSREF_SEARCH_URL]
    assert openalex.calls == [OPENALEX_SEARCH_URL]


def test_fetch_by_title_rejects_blank_title_before_any_http_call():
    client, crossref, openalex = client_with({}, {})
    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch_by_title("   "))
    assert caught.value.kind is FailureKind.PARSE
    assert crossref.calls == [] and openalex.calls == []


def test_fetch_by_title_failure_never_echoes_link_credentials():
    """A token-bearing candidate link is redacted from the no-match error."""
    client, crossref, openalex = client_with(
        {CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([]))},
        {OPENALEX_SEARCH_URL: response(OPENALEX_SEARCH_URL, {"results": []})},
    )
    with pytest.raises(SourceFetchError) as caught:
        run(
            client.fetch_by_title(
                TITLE,
                link="https://www.nature.com/articles/xyz?token=sekrit&api_key=abc123",
            )
        )
    assert caught.value.kind is FailureKind.PARSE
    assert "sekrit" not in str(caught.value)
    assert "abc123" not in str(caught.value)
    assert caught.value.url == (
        "https://www.nature.com/articles/xyz?token=<redacted>&api_key=<redacted>"
    )

    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch_by_title(TITLE, link="https://user:secret@nature.com/xyz"))
    assert "user:secret" not in str(caught.value)
    assert caught.value.url == "https://nature.com/xyz"


def test_fetch_by_title_item_is_never_fulltext():
    abstractless = crossref_item(TITLE)
    abstractless.pop("abstract", None)
    client, crossref, _ = client_with(
        {CROSSREF_SEARCH_URL: response(CROSSREF_SEARCH_URL, crossref_search([abstractless]))},
        {},
    )
    item = run(client.fetch_by_title(TITLE))
    assert item.content is None
    assert item.summary is None
    assert item.access_level == "metadata_only"
    assert extract_doi(item.link) == DOI
