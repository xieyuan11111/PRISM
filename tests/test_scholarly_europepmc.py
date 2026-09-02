"""Europe PMC fallback for PMID/PMCID candidates (offline, injected HTTP)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from prism.api.fetching import spool_source_item
from prism.sources import (
    AcademicRecord,
    CrossrefClient,
    EuropePmcClient,
    FailureKind,
    HttpResponse,
    OpenAlexClient,
    ScholarlyMetadataClient,
    SourceFetchError,
    SourceItem,
    extract_pmcid,
    extract_pmid,
    normalize_pmcid,
    normalize_pmid,
)

UTC = timezone.utc
RETRIEVED = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
PMID = "40212345"
PMCID = "PMC8880123"
DOI = "10.1000/example.42"
PUBMED_URL = f"https://pubmed.ncbi.nlm.nih.gov/{PMID}/"
PMC_URL = f"https://pmc.ncbi.nlm.nih.gov/articles/{PMCID}/"
EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPEPMC_PMID_URL = (
    f"{EUROPEPMC_BASE}?query=EXT_ID%3A{PMID}%20AND%20SRC%3AMED&format=json&resultType=core"
)
EUROPEPMC_PMCID_URL = f"{EUROPEPMC_BASE}?query=PMCID%3A{PMCID}&format=json&resultType=core"


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


def europepmc_payload(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": PMID,
        "source": "MED",
        "pmid": PMID,
        "pmcid": PMCID,
        "title": "Public evidence for a fallback",
        "authorString": "Lovelace A, Hopper G.",
        "journalTitle": "Journal of Public Evidence",
        "pubYear": "2024",
        "firstPublicationDate": "2024-03-04",
        "doi": DOI,
        "abstractText": "A public abstract retrieved without credentials.",
    }
    result.update(overrides)
    return {"version": "4.9", "hitCount": 1, "request": {}, "resultList": {"result": [result]}}


def run(coro):
    return asyncio.run(coro)


def record(**kwargs: object) -> AcademicRecord:
    base: dict[str, object] = {
        "title": "T",
        "source": "academic",
        "link": "https://doi.org/10.1000/example.42",
        "retrieved_at": RETRIEVED,
        "published_at": None,
        "authors": (),
        "doi": DOI,
        "abstract": None,
        "access_level": "metadata_only",
    }
    base.update(kwargs)
    return AcademicRecord(**base)  # type: ignore[arg-type]


def test_extract_pmid_from_pubmed_urls_and_explicit_prefix_only():
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov/40212345/") == "40212345"
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov/40212345/?report=docsummary") == "40212345"
    assert extract_pmid("https://www.ncbi.nlm.nih.gov/pubmed/40212345/") == "40212345"
    assert extract_pmid("PMID: 40212345") == "40212345"
    # Bare digits anywhere on the web are not a trustworthy identifier.
    assert extract_pmid("https://example.gov/articles/40212345") is None
    assert extract_pmid("40212345") is None
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov/402123451234/") is None  # too long


def test_extract_pmcid_from_pmc_urls_and_explicit_prefix_only():
    assert extract_pmcid("https://pmc.ncbi.nlm.nih.gov/articles/PMC8880123/") == "PMC8880123"
    assert extract_pmcid("https://www.ncbi.nlm.nih.gov/pmc/articles/pmc8880123/") == "PMC8880123"
    assert extract_pmcid("PMCID: PMC8880123") == "PMC8880123"
    assert extract_pmcid("https://example.gov/articles/PMC8880123") is None
    assert extract_pmcid("https://pmc.ncbi.nlm.nih.gov/articles/8880123/") is None  # no PMC prefix


def test_extract_pmid_ignores_identifiers_embedded_in_foreign_urls():
    # A PMID embedded in another site's path, query, fragment, or userinfo
    # must never be read: only genuine PubMed URLs count, with the exact
    # hostname and an anchored article path.
    assert extract_pmid("https://evil.example/pubmed.ncbi.nlm.nih.gov/40212345") is None
    assert extract_pmid("https://evil.example/redirect?next=https://pubmed.ncbi.nlm.nih.gov/40212345") is None
    assert extract_pmid("https://evil.example/?u=https%3A%2F%2Fpubmed.ncbi.nlm.nih.gov%2F40212345") is None
    assert extract_pmid("https://evil.example/articles/40212345") is None
    assert extract_pmid("https://evil.example/?q=PMID%3A+40212345") is None
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov.evil.example/40212345") is None
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov@evil.example/40212345") is None  # userinfo trick
    assert extract_pmid("https://evil.example@pubmed.ncbi.nlm.nih.gov/40212345") is None
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov/40212345/extra") is None
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov/pubmed/40212345") is None  # wrong host shape
    assert extract_pmid("https://www.ncbi.nlm.nih.gov/40212345") is None
    assert extract_pmid("see PMID: 40212345 for details") is None  # literal, not prose
    # Genuine PubMed URL variants still resolve.
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov/40212345") == "40212345"
    assert extract_pmid("https://www.ncbi.nlm.nih.gov/pubmed/40212345") == "40212345"


def test_extract_pmcid_ignores_identifiers_embedded_in_foreign_urls():
    assert extract_pmcid("https://evil.example/pmc.ncbi.nlm.nih.gov/articles/PMC8880123") is None
    assert extract_pmcid("https://evil.example/?next=https://pmc.ncbi.nlm.nih.gov/articles/PMC8880123") is None
    assert extract_pmcid("https://pmc.ncbi.nlm.nih.gov.evil.example/articles/PMC8880123") is None
    assert extract_pmcid("https://pmc.ncbi.nlm.nih.gov@evil.example/articles/PMC8880123") is None
    assert extract_pmcid("https://evil.example@pmc.ncbi.nlm.nih.gov/articles/PMC8880123") is None
    assert extract_pmcid("https://pmc.ncbi.nlm.nih.gov/articles/PMC8880123/extra") is None
    assert extract_pmcid("https://pmc.ncbi.nlm.nih.gov/pmc/articles/PMC8880123") is None
    assert extract_pmcid("https://www.ncbi.nlm.nih.gov/articles/PMC8880123") is None
    assert extract_pmcid("PMC8880123") is None  # bare digits/PMC strings are not URLs
    assert extract_pmcid("see PMCID: PMC8880123 in text") is None
    # Genuine PMC URL variants still resolve.
    assert extract_pmcid("https://pmc.ncbi.nlm.nih.gov/articles/PMC8880123") == "PMC8880123"
    assert extract_pmcid("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8880123/") == "PMC8880123"


def test_normalize_pmid_and_pmcid_canonicalize():
    assert normalize_pmid("PMID: 40212345") == "40212345"
    assert normalize_pmid("40212345") == "40212345"
    with pytest.raises(ValueError):
        normalize_pmid("not-a-pmid")
    assert normalize_pmcid("pmcid:pmc8880123") == "PMC8880123"
    assert normalize_pmcid("PMC8880123") == "PMC8880123"
    with pytest.raises(ValueError):
        normalize_pmcid("8880123")


def test_academic_record_accepts_identifier_alternatives_and_requires_one():
    pmid_only = record(doi=None, pmid=PMID)
    assert pmid_only.doi is None
    assert pmid_only.pmid == PMID
    pmcid_only = record(doi=None, pmcid="pmc8880123")
    assert pmcid_only.pmcid == PMCID
    both = record(pmid="40212345", pmcid=PMCID)
    assert both.pmid == PMID and both.pmcid == PMCID
    with pytest.raises(ValueError):
        record(doi=None)  # no identifier at all
    with pytest.raises(ValueError):
        record(doi=None, pmid="12345678901")  # 10 digits is not a PMID


def test_europepmc_pmid_record_maps_public_metadata_honestly():
    getter = FakeGetter({EUROPEPMC_PMID_URL: response(EUROPEPMC_PMID_URL, europepmc_payload())})
    rec = run(EuropePmcClient(getter).fetch(PMID, retrieved_at=RETRIEVED))
    assert rec.title == "Public evidence for a fallback"
    assert rec.abstract == "A public abstract retrieved without credentials."
    assert rec.access_level == "abstract_only"
    assert rec.authors == ("Lovelace A", "Hopper G")
    assert rec.container_title == "Journal of Public Evidence"
    assert rec.published_at == datetime(2024, 3, 4, tzinfo=UTC)
    assert rec.doi == DOI
    assert rec.pmid == PMID
    assert rec.pmcid == PMCID
    assert rec.link == "https://doi.org/10.1000%2Fexample.42"
    assert getter.calls == [EUROPEPMC_PMID_URL]


def test_europepmc_pmcid_record_without_doi_uses_pmc_link():
    payload = europepmc_payload(
        doi=None,
        abstractText=None,
        pmcid=PMCID,
    )
    getter = FakeGetter({EUROPEPMC_PMCID_URL: response(EUROPEPMC_PMCID_URL, payload)})
    rec = run(EuropePmcClient(getter).fetch("PMCID:PMC8880123", retrieved_at=RETRIEVED))
    assert rec.doi is None
    assert rec.abstract is None
    assert rec.access_level == "metadata_only"
    assert rec.link == PMC_URL
    item = rec.to_source_item()
    assert item.pmid == PMID
    assert item.pmcid == PMCID
    assert item.doi is None
    metadata = item.to_ingestion_metadata()
    assert metadata["pmid"] == PMID
    assert metadata["pmcid"] == PMCID
    assert "doi" not in metadata


def test_europepmc_pubyear_fallback_dates_to_january_first():
    payload = europepmc_payload(firstPublicationDate=None, pubYear="2023")
    getter = FakeGetter({EUROPEPMC_PMID_URL: response(EUROPEPMC_PMID_URL, payload)})
    rec = run(EuropePmcClient(getter).fetch(PMID, retrieved_at=RETRIEVED))
    assert rec.published_at == datetime(2023, 1, 1, tzinfo=UTC)


def test_europepmc_no_hit_is_classified_as_parse():
    payload = {"hitCount": 0, "resultList": {"result": []}}
    getter = FakeGetter({EUROPEPMC_PMID_URL: response(EUROPEPMC_PMID_URL, payload)})
    with pytest.raises(SourceFetchError) as caught:
        run(EuropePmcClient(getter).fetch(PMID, retrieved_at=RETRIEVED))
    assert caught.value.kind is FailureKind.PARSE
    assert getter.calls == [EUROPEPMC_PMID_URL]


def test_europepmc_record_for_another_identifier_is_rejected():
    payload = europepmc_payload(pmid="999")  # not the requested PMID
    getter = FakeGetter({EUROPEPMC_PMID_URL: response(EUROPEPMC_PMID_URL, payload)})
    with pytest.raises(SourceFetchError) as caught:
        run(EuropePmcClient(getter).fetch(PMID, retrieved_at=RETRIEVED))
    assert caught.value.kind is FailureKind.PARSE


def test_europepmc_foreign_response_host_is_blocked():
    # The getter answers the approved URL, but with a response whose own URL
    # points elsewhere — the shared host gate must reject it.
    hijacked = HttpResponse("https://evil.example/search", 200, json.dumps(europepmc_payload()))
    getter = FakeGetter({EUROPEPMC_PMID_URL: hijacked})
    with pytest.raises(SourceFetchError) as caught:
        run(EuropePmcClient(getter).fetch(PMID, retrieved_at=RETRIEVED))
    assert caught.value.kind is FailureKind.BLOCKED


def test_europepmc_malformed_payload_is_classified_as_parse():
    getter = FakeGetter({EUROPEPMC_PMID_URL: HttpResponse(EUROPEPMC_PMID_URL, 200, "not-json")})
    with pytest.raises(SourceFetchError) as caught:
        run(EuropePmcClient(getter).fetch(PMID, retrieved_at=RETRIEVED))
    assert caught.value.kind is FailureKind.PARSE


def test_scholarly_client_routes_pubmed_url_to_europepmc_only():
    europepmc = FakeGetter({EUROPEPMC_PMID_URL: response(EUROPEPMC_PMID_URL, europepmc_payload())})
    crossref = FakeGetter({})
    openalex = FakeGetter({})
    client = ScholarlyMetadataClient(
        CrossrefClient(crossref),
        OpenAlexClient(openalex),
        EuropePmcClient(europepmc),
        clock=lambda: RETRIEVED,
    )
    item = run(client.fetch(PUBMED_URL))
    assert item.title == "Public evidence for a fallback"
    assert item.summary == "A public abstract retrieved without credentials."
    assert item.content is None
    assert item.access_level == "abstract_only"
    assert item.pmid == PMID
    assert europepmc.calls == [EUROPEPMC_PMID_URL]
    assert crossref.calls == []
    assert openalex.calls == []


def test_scholarly_client_routes_pmc_url_to_europepmc():
    europepmc = FakeGetter({EUROPEPMC_PMCID_URL: response(EUROPEPMC_PMCID_URL, europepmc_payload())})
    client = ScholarlyMetadataClient(
        CrossrefClient(FakeGetter({})),
        OpenAlexClient(FakeGetter({})),
        EuropePmcClient(europepmc),
        clock=lambda: RETRIEVED,
    )
    item = run(client.fetch(PMC_URL))
    assert item.pmcid == PMCID
    assert europepmc.calls == [EUROPEPMC_PMCID_URL]


def test_doi_in_url_still_takes_the_crossref_path_before_pubmed_routing():
    crossref = FakeGetter({
        "https://api.crossref.org/works/10.1000%2Fexample.42": response(
            "https://api.crossref.org/works/10.1000%2Fexample.42",
            {"message": {"title": ["DOI wins"], "DOI": DOI}},
        )
    })
    europepmc = FakeGetter({})
    client = ScholarlyMetadataClient(
        CrossrefClient(crossref),
        OpenAlexClient(FakeGetter({})),
        EuropePmcClient(europepmc),
        clock=lambda: RETRIEVED,
    )
    item = run(client.fetch(f"{PMC_URL}?doi=10.1000/example.42"))
    assert item.title == "DOI wins"
    assert europepmc.calls == []


def test_scholarly_client_without_europepmc_rejects_pmid_input_honestly():
    crossref = FakeGetter({})
    openalex = FakeGetter({})
    client = ScholarlyMetadataClient(
        CrossrefClient(crossref),
        OpenAlexClient(openalex),
        clock=lambda: RETRIEVED,
    )
    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch(PUBMED_URL))
    assert caught.value.kind is FailureKind.PARSE
    assert crossref.calls == [] and openalex.calls == []


def test_identifier_failure_errors_never_echo_url_credentials():
    """Identifier failure contexts redact tokens, api keys, and userinfo."""
    crossref = FakeGetter({})
    openalex = FakeGetter({})
    client = ScholarlyMetadataClient(
        CrossrefClient(crossref),
        OpenAlexClient(openalex),
        clock=lambda: RETRIEVED,
    )
    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch("https://evil.example/paper?token=sekrit&api_key=abc123"))
    assert caught.value.kind is FailureKind.PARSE
    assert caught.value.url == "https://evil.example/paper?token=<redacted>&api_key=<redacted>"
    assert "sekrit" not in str(caught.value)
    assert "abc123" not in str(caught.value)

    with pytest.raises(SourceFetchError) as caught:
        run(client.fetch("https://user:secret@evil.example/paper"))
    assert caught.value.kind is FailureKind.PARSE
    assert caught.value.url == "https://evil.example/paper"
    assert "user:secret" not in str(caught.value)
    assert crossref.calls == [] and openalex.calls == []


def test_europepmc_identifier_failure_redacts_credentials():
    getter = FakeGetter({})
    with pytest.raises(SourceFetchError) as caught:
        run(
            EuropePmcClient(getter).fetch(
                "https://evil.example/paper?token=sekrit", retrieved_at=RETRIEVED
            )
        )
    assert caught.value.kind is FailureKind.PARSE
    assert caught.value.url == "https://evil.example/paper?token=<redacted>"
    assert "sekrit" not in str(caught.value)
    assert getter.calls == []


def test_source_item_validates_and_exports_pubmed_identifiers():
    item = SourceItem(
        title="A paper",
        source="academic",
        fetched_at=RETRIEVED,
        type="academic",
        access_level="metadata_only",
        pmid=PMID,
        pmcid=PMCID,
    )
    assert item.pmid == PMID
    assert item.pmcid == PMCID
    metadata = item.to_ingestion_metadata()
    assert metadata["pmid"] == PMID
    assert metadata["pmcid"] == PMCID
    with pytest.raises(ValueError):
        SourceItem(
            title="A paper",
            source="academic",
            fetched_at=RETRIEVED,
            pmid="   ",
        )


def test_metadata_only_spool_body_lists_pubmed_identifiers(tmp_path):
    item = SourceItem(
        title="A PMC paper",
        source="academic",
        fetched_at=RETRIEVED,
        link=PMC_URL,
        type="academic",
        access_level="metadata_only",
        retrieval_level="metadata_only",
        pmid=PMID,
        pmcid=PMCID,
        authors=("Lovelace A",),
    )
    text = spool_source_item(item, tmp_path).read_text(encoding="utf-8")
    assert "PMCID: PMC8880123" in text
    assert "PMID: 40212345" in text
    assert "Full text was not available" in text
