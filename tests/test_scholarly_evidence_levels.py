from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prism.api.fetching import spool_source_item
from prism.sources import FailureKind, SourceFetchError, SourceItem, extract_page

UTC = timezone.utc
NOW = datetime(2026, 9, 2, tzinfo=UTC)


def test_access_verification_page_is_not_accepted_as_evidence():
    with pytest.raises(SourceFetchError) as caught:
        extract_page(
            "<html><head><title>Checking your browser</title></head>"
            "<body>Please complete the CAPTCHA to continue.</body></html>",
            url="https://pmc.ncbi.nlm.nih.gov/articles/PMC1",
            fetched_at=NOW,
        )
    assert caught.value.kind is FailureKind.BLOCKED


def test_public_page_extraction_is_labeled_fulltext():
    item = extract_page(
        "<html><head><title>A public article</title></head>"
        "<body><p>The full public body of the article.</p></body></html>",
        url="https://example.gov/articles/1",
        fetched_at=NOW,
    )
    assert item.content is not None
    assert item.access_level == "fulltext"
    assert item.retrieval_level == "fulltext"
    assert item.to_ingestion_metadata()["access_level"] == "fulltext"


def test_metadata_only_item_gets_honest_bibliographic_spool_body(tmp_path):
    item = SourceItem(
        title="A paper",
        source="academic",
        fetched_at=NOW,
        link="https://doi.org/10.1000/example",
        type="academic",
        access_level="metadata_only",
        retrieval_level="metadata_only",
        doi="10.1000/example",
        authors=("Ada Lovelace", "Grace Hopper"),
        container_title="Journal of Evidence",
    )

    path = spool_source_item(item, tmp_path)

    text = path.read_text(encoding="utf-8")
    assert "A paper" in text
    assert "10.1000/example" in text
    assert "Ada Lovelace, Grace Hopper" in text
    assert "Journal of Evidence" in text
    assert "Full text was not available" in text


def test_long_article_mentioning_captcha_is_not_rejected_as_a_wall():
    body = (
        "<html><head><title>Captcha research survey</title></head><body>"
        + ("<p>This article discusses CAPTCHA usability in depth " * 120)
        + "</p></body></html>"
    )
    item = extract_page(body, url="https://example.gov/articles/1", fetched_at=NOW)
    assert item.access_level == "fulltext"
    assert len(item.content or "") >= 2000


def test_article_quoting_wall_phrasing_below_the_lead_is_not_a_wall():
    # A short legitimate report whose lead is real content and only later
    # quotes "Access denied" must not be classified as a verification wall.
    lead = (
        "<p>The agency portal remained reachable throughout the incident, "
        "and press updates continued on schedule while engineers worked. "
    ) * 6
    body = (
        "<html><head><title>Portal outage explained</title></head><body>"
        + lead
        + "<p>Users who saw 'Access denied' during the outage were advised "
        "to sign in again; the issue was unrelated to their accounts.</p>"
        "</body></html>"
    )
    item = extract_page(body, url="https://example.gov/news/outage", fetched_at=NOW)
    assert item.access_level == "fulltext"
    assert len(item.content or "") < 2000
    assert "Access denied" in (item.content or "")
