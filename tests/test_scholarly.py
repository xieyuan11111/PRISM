from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from prism.sources import (
    AcademicRecord,
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
DOI = "10.5555/example.123"
DOI_URL = "https://doi.org/10.5555%2Fexample.123"
CROSSREF_URL = "https://api.crossref.org/works/10.5555%2Fexample.123"
OPENALEX_URL = "https://api.openalex.org/works/https://doi.org/10.5555%2Fexample.123"


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


def test_extract_doi_from_doi_url_and_article_url():
    assert extract_doi("https://doi.org/10.1000/ABC.42") == "10.1000/abc.42"
    assert extract_doi("https://journal.example/articles/10.1000/ABC.42?x=1") == "10.1000/abc.42"
    assert extract_doi("doi:10.1000/ABC.42.") == "10.1000/abc.42"


def test_crossref_success_strips_jats_and_keeps_abstract_out_of_content():
    getter = FakeGetter({
        CROSSREF_URL: response(CROSSREF_URL, {"message": {
            "title": ["A study"], "container-title": ["Journal"],
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "published": {"date-parts": [[2024, 3, 4]]}, "DOI": DOI,
            "abstract": "<jats:p>Evidence <jats:italic>supports</jats:italic> it.</jats:p>",
            "URL": "https://journal.example/article/123",
        }})
    })
    record = run(CrossrefClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    item = record.to_source_item()
    assert isinstance(record, AcademicRecord)
    assert record.source == "academic"
    assert record.container_title == "Journal"
    assert record.authors == ("Ada Lovelace",)
    assert record.published_at == datetime(2024, 3, 4, tzinfo=UTC)
    assert record.abstract == "Evidence supports it."
    assert record.access_level == "abstract_only"
    assert item.summary == record.abstract
    assert item.content is None
    assert item.source == "academic"
    assert item.access_level == "abstract_only"
    assert item.to_ingestion_metadata()["access_level"] == "abstract_only"


def test_record_to_source_item_keeps_authors_container_and_doi():
    getter = FakeGetter({
        CROSSREF_URL: response(CROSSREF_URL, {"message": {
            "title": ["A study"], "container-title": ["Journal of Evidence"],
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "published": {"date-parts": [[2024, 3, 4]]}, "DOI": DOI,
            "abstract": "<jats:p>Evidence supports it.</jats:p>",
        }})
    })
    item = run(CrossrefClient(getter).fetch(DOI, retrieved_at=RETRIEVED)).to_source_item()

    assert item.authors == ("Ada Lovelace",)
    assert item.container_title == "Journal of Evidence"
    assert item.doi == DOI
    metadata = item.to_ingestion_metadata()
    assert metadata["authors"] == ["Ada Lovelace"]
    assert metadata["container_title"] == "Journal of Evidence"
    assert metadata["doi"] == DOI


def test_crossref_without_abstract_is_metadata_only():
    getter = FakeGetter({
        CROSSREF_URL: response(CROSSREF_URL, {"message": {
            "title": ["Metadata only"], "DOI": DOI,
            "published-print": {"date-parts": [[2020]]},
        }})
    })
    record = run(CrossrefClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    assert record.abstract is None
    assert record.access_level == "metadata_only"
    assert record.to_source_item().content is None


def test_openalex_is_fallback_when_crossref_fails_and_reconstructs_abstract():
    crossref = FakeGetter({CROSSREF_URL: response(CROSSREF_URL, [], status=200)})
    openalex = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {"title": "OA title",
            "publication_date": "2023-11-02",
            "authorships": [{"author": {"display_name": "Grace Hopper"}}],
            "ids": {"doi": "https://doi.org/" + DOI},
            "abstract_inverted_index": {"text": [1], "A": [0]},
        })
    })
    client = ScholarlyMetadataClient(CrossrefClient(crossref), OpenAlexClient(openalex), clock=lambda: RETRIEVED)
    item = run(client.fetch("https://journal.example/paper/" + DOI))
    assert item.title == "OA title"
    assert item.summary == "A text"
    assert item.content is None
    assert item.access_level == "abstract_only"
    assert crossref.calls == [CROSSREF_URL]
    assert openalex.calls == [OPENALEX_URL]


def test_openalex_abstract_merges_into_crossref_record_without_replacing_it():
    """An OpenAlex abstract upgrades Crossref metadata; it never replaces it.

    Regression: enrichment used to swap in the whole OpenAlex record, which
    discarded the Crossref title.  The merge keeps the Crossref identity and
    only adds the abstract, with OpenAlex queried at most once per DOI.
    """
    crossref = FakeGetter({
        CROSSREF_URL: response(CROSSREF_URL, {"message": {
            "title": ["No abstract"], "container-title": ["Journal"],
            "DOI": DOI,
        }})
    })
    openalex = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {"title": "OA enriched",
            "ids": {"doi": "https://doi.org/" + DOI},
            "abstract_inverted_index": {"evidence": [0], "Public": [1]},
        })
    })
    client = ScholarlyMetadataClient(CrossrefClient(crossref), OpenAlexClient(openalex), clock=lambda: RETRIEVED)
    item = run(client.fetch(DOI_URL))
    assert item.title == "No abstract"  # Crossref title survives the merge
    assert item.summary == "evidence Public"
    assert item.access_level == "abstract_only"
    assert crossref.calls == [CROSSREF_URL]
    assert openalex.calls == [OPENALEX_URL]


def test_openalex_enrichment_keeps_rich_crossref_bibliography():
    """Crossref authors/venue/link/date survive an OpenAlex abstract merge.

    Regression for the review finding: when Crossref metadata_only already
    carries authors/container_title and OpenAlex enrichment returns an
    abstract, the Crossref bibliography must not be discarded wholesale.
    """
    crossref = FakeGetter({
        CROSSREF_URL: response(CROSSREF_URL, {"message": {
            "title": ["Crossref title"],
            "container-title": ["Journal of Evidence"],
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "published": {"date-parts": [[2024, 3, 4]]},
            "DOI": DOI,
            "URL": "https://journal.example/article/123",
        }})
    })
    openalex = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {
            "title": "OA title",
            "publication_date": "2023-11-02",
            "authorships": [{"author": {"display_name": "Grace Hopper"}}],
            "ids": {"doi": "https://doi.org/10.5555/other.999"},
            "primary_location": {"source": {"display_name": "OA Journal"}},
            "abstract_inverted_index": {"An": [0], "abstract": [1]},
        })
    })
    client = ScholarlyMetadataClient(CrossrefClient(crossref), OpenAlexClient(openalex), clock=lambda: RETRIEVED)
    item = run(client.fetch(DOI_URL))
    assert item.title == "Crossref title"
    assert item.doi == DOI
    assert item.authors == ("Ada Lovelace",)
    assert item.container_title == "Journal of Evidence"
    assert item.link == "https://journal.example/article/123"
    assert item.published_at == datetime(2024, 3, 4, tzinfo=UTC)
    assert item.summary == "An abstract"  # the only OpenAlex contribution
    assert item.access_level == "abstract_only"
    assert crossref.calls == [CROSSREF_URL]
    assert openalex.calls == [OPENALEX_URL]


def test_openalex_enrichment_fills_fields_crossref_lacks():
    """A bare Crossref placeholder is upgraded, not replaced, by OpenAlex."""
    crossref = FakeGetter({
        CROSSREF_URL: response(CROSSREF_URL, {"message": {
            "title": ["Bare placeholder"], "DOI": DOI,
        }})
    })
    openalex = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {
            "title": "OA title",
            "publication_date": "2023-11-02",
            "authorships": [{"author": {"display_name": "Grace Hopper"}}],
            "ids": {"doi": "https://doi.org/" + DOI},
            "primary_location": {
                "source": {"display_name": "OA Journal of Evidence"},
                "landing_page_url": "https://oa.example/landing",
            },
            "abstract_inverted_index": {"Public": [0], "abstract": [1]},
        })
    })
    client = ScholarlyMetadataClient(CrossrefClient(crossref), OpenAlexClient(openalex), clock=lambda: RETRIEVED)
    item = run(client.fetch(DOI_URL))
    assert item.title == "Bare placeholder"
    assert item.doi == DOI
    assert item.authors == ("Grace Hopper",)
    assert item.container_title == "OA Journal of Evidence"
    assert item.published_at == datetime(2023, 11, 2, tzinfo=UTC)
    assert item.summary == "Public abstract"
    assert item.access_level == "abstract_only"
    assert crossref.calls == [CROSSREF_URL]
    assert openalex.calls == [OPENALEX_URL]


def test_openalex_is_never_queried_twice_for_one_doi():
    """Crossref failure + abstractless OpenAlex fallback must not re-query."""
    crossref = FakeGetter({CROSSREF_URL: response(CROSSREF_URL, [], status=200)})
    openalex = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {"title": "OA bare",
            "ids": {"doi": "https://doi.org/" + DOI},
        })
    })
    client = ScholarlyMetadataClient(CrossrefClient(crossref), OpenAlexClient(openalex), clock=lambda: RETRIEVED)
    item = run(client.fetch(DOI_URL))
    assert item.title == "OA bare"
    assert item.summary is None
    assert item.access_level == "metadata_only"
    assert crossref.calls == [CROSSREF_URL]
    assert openalex.calls == [OPENALEX_URL]


def test_failed_enrichment_keeps_crossref_record_and_queries_openalex_once():
    """Crossref metadata without abstract + failing OpenAlex keeps Crossref."""
    crossref = FakeGetter({
        CROSSREF_URL: response(CROSSREF_URL, {"message": {
            "title": ["Crossref record"], "DOI": DOI,
        }})
    })
    openalex = FakeGetter({
        OPENALEX_URL: SourceFetchError(
            FailureKind.TRANSPORT, OPENALEX_URL, "openalex down"
        )
    })
    client = ScholarlyMetadataClient(CrossrefClient(crossref), OpenAlexClient(openalex), clock=lambda: RETRIEVED)
    item = run(client.fetch(DOI_URL))
    assert item.title == "Crossref record"
    assert item.access_level == "metadata_only"
    assert crossref.calls == [CROSSREF_URL]
    assert openalex.calls == [OPENALEX_URL]


def test_bare_openalex_record_does_not_replace_richer_crossref_metadata():
    """OpenAlex without an abstract must not displace Crossref bibliography.

    The merge rule only applies when the OpenAlex abstract is non-empty: a
    bare OpenAlex record adds nothing — not even a venue — and the Crossref
    metadata-only record stays exactly as it was.
    """
    crossref = FakeGetter({
        CROSSREF_URL: response(CROSSREF_URL, {"message": {
            "title": ["Crossref record"],
            "container-title": ["Journal of Evidence"],
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "DOI": DOI,
        }})
    })
    openalex = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {"title": "OA bare",
            "ids": {"doi": "https://doi.org/" + DOI},
            "primary_location": {"source": {"display_name": "OA Journal"}},
        })
    })
    client = ScholarlyMetadataClient(CrossrefClient(crossref), OpenAlexClient(openalex), clock=lambda: RETRIEVED)
    item = run(client.fetch(DOI_URL))
    assert item.title == "Crossref record"
    assert item.access_level == "metadata_only"
    assert item.doi == DOI
    assert item.container_title == "Journal of Evidence"  # not the OA venue
    assert item.authors == ("Ada Lovelace",)
    assert crossref.calls == [CROSSREF_URL]
    assert openalex.calls == [OPENALEX_URL]


def test_both_provider_failures_preserve_the_crossref_classification():
    """Crossref failure + OpenAlex failure reports the primary failure kind."""
    crossref = FakeGetter({
        CROSSREF_URL: SourceFetchError(
            FailureKind.HTTP_STATUS, CROSSREF_URL, "HTTP 404"
        )
    })
    openalex = FakeGetter({
        OPENALEX_URL: SourceFetchError(
            FailureKind.TIMEOUT, OPENALEX_URL, "openalex timed out"
        )
    })
    client = ScholarlyMetadataClient(CrossrefClient(crossref), OpenAlexClient(openalex), clock=lambda: RETRIEVED)
    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch(DOI_URL))
    assert caught.value.kind is FailureKind.HTTP_STATUS
    assert caught.value.url == CROSSREF_URL
    assert crossref.calls == [CROSSREF_URL]
    assert openalex.calls == [OPENALEX_URL]


def test_openalex_record_carries_venue_from_primary_location_source():
    """OpenAlex venue comes from primary_location.source.display_name."""
    getter = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {
            "title": "With venue",
            "publication_date": "2023-11-02",
            "authorships": [{"author": {"display_name": "Grace Hopper"}}],
            "ids": {"doi": "https://doi.org/" + DOI},
            "primary_location": {"source": {"display_name": "OA Journal of Evidence"}},
            "abstract_inverted_index": {"A": [0], "text": [1]},
        })
    })
    record = run(OpenAlexClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    assert record.container_title == "OA Journal of Evidence"
    assert record.access_level == "abstract_only"


def test_openalex_without_venue_or_abstract_is_metadata_only():
    getter = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {
            "title": "Bare",
            "ids": {"doi": "https://doi.org/" + DOI},
            "primary_location": None,
        })
    })
    record = run(OpenAlexClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    assert record.container_title is None
    assert record.access_level == "metadata_only"


def test_malformed_openalex_primary_location_is_classified_as_parse():
    getter = FakeGetter({
        OPENALEX_URL: response(OPENALEX_URL, {
            "title": "Bad location",
            "ids": {"doi": "https://doi.org/" + DOI},
            "primary_location": "not-an-object",
        })
    })
    with pytest.raises(SourceFetchError) as caught:
        run(OpenAlexClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    assert caught.value.kind is FailureKind.PARSE


class HeaderCapturingGetter:
    def __init__(self, http_response) -> None:
        self.http_response = http_response
        self.calls: list[dict[str, object]] = []

    async def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append({"url": url, "timeout": timeout})
        return self.http_response


def test_metadata_clients_send_bare_public_get_without_authorization():
    getter = HeaderCapturingGetter(
        response(CROSSREF_URL, {"message": {"title": ["T"], "DOI": DOI}})
    )
    run(CrossrefClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    assert len(getter.calls) == 1
    assert set(getter.calls[0]) == {"url", "timeout"}
    assert getter.calls[0]["url"] == CROSSREF_URL
    assert "headers" not in getter.calls[0]
    assert "Authorization" not in str(getter.calls[0])


def test_malformed_response_is_classified_as_parse_without_secret():
    getter = FakeGetter({CROSSREF_URL: HttpResponse(CROSSREF_URL, 200, "not-json")})
    with pytest.raises(SourceFetchError) as caught:
        run(CrossrefClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    assert caught.value.kind is FailureKind.PARSE
    assert "Authorization" not in str(caught.value)


def test_no_doi_is_classified_and_no_authorization_is_sent():
    getter = FakeGetter({})
    client = ScholarlyMetadataClient(CrossrefClient(getter), OpenAlexClient(getter), clock=lambda: RETRIEVED)
    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch("https://journal.example/articles/no-identifier"))
    assert caught.value.kind is FailureKind.PARSE
    assert getter.calls == []


def test_getter_source_error_does_not_leak_authorization():
    getter = FakeGetter({CROSSREF_URL: SourceFetchError(FailureKind.TRANSPORT, CROSSREF_URL, "Authorization: secret")})
    with pytest.raises(SourceFetchError) as caught:
        run(CrossrefClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    assert "Authorization" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_metadata_clients_make_public_gets_without_authorization_header():
    getter = FakeGetter({CROSSREF_URL: response(CROSSREF_URL, {"message": {"title": ["T"], "DOI": DOI}})})
    run(CrossrefClient(getter).fetch(DOI, retrieved_at=RETRIEVED))
    assert getter.calls == [CROSSREF_URL]
