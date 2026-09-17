"""Real-run regressions for scholarly API discovery leads (2026-09-16).

The HN-AD review research run surfaced Europe PMC REST endpoints as discovery
leads (``.../webservices/rest/search?query=EXT_ID:...`` /
``...?query=PMCID:...``) and the fetch stage tried to ingest those API URLs as
if they were articles: one failed with ``timeout``, two with ``http_status``.
A scholarly *API* lead must be resolved to the canonical article URL through
the existing scholarly adapter — or dropped with an auditable failure record —
and must never reach the fetcher as an article.  Identifiers are only ever
read from the approved REST endpoints themselves (host and anchored path),
never from arbitrary URLs, mirroring the ``extract_pmid``/``extract_pmcid``
discipline in :mod:`prism.sources.scholarly`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.api import PrismAPI
from prism.api.fetching import SourceFetchReport, SourceItemReport
from prism.research import (
    CANDIDATE_UNRESOLVED_LEAD,
    ResearchExecutor,
    ResearchPlan,
    ResearchWindow,
    SearchQuery,
    SourceCandidate,
    scholarly_api_identifier,
)
from prism.sources import (
    FailureKind,
    SourceFetchError,
    SourceItem,
    normalize_url,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

EBI = "www.ebi.ac.uk"
REST_SEARCH_EXT_ID = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    "?query=EXT_ID%3A41349773%20AND%20SRC%3AMED&format=json&resultType=core"
)
REST_SEARCH_PMCID = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    "?query=PMCID%3APMC11577200&format=json"
)
DOI_ARTICLE = "https://doi.org/10.1007/s00449-023-02854-9"
PMC_ARTICLE = "https://pmc.ncbi.nlm.nih.gov/articles/PMC11577200/"


def window():
    return ResearchWindow(
        phase="publication",
        start_at=datetime(2025, 6, 1, tzinfo=UTC),
        end_at=datetime(2025, 9, 1, tzinfo=UTC),
        focus="Find the original publication.",
    )


def plan_for(query_text, source_domains=(EBI,), candidates=None):
    return ResearchPlan(
        source_id="material-7",
        anchor_at=datetime(2025, 6, 1, tzinfo=UTC),
        frontier_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
        planned_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        origin="fallback",
        windows=(window(),),
        candidates=candidates or (
            SourceCandidate(
                domain=EBI,
                source_types=("academic_paper",),
                priority=1,
                reason="Whitelisted scholarly host.",
            ),
        ),
        queries=(
            SearchQuery(
                query=query_text,
                window=window(),
                source_types=("academic_paper",),
                source_domains=source_domains,
                reason="Locate the article.",
            ),
        ),
    )


def lead(url):
    return SourceItem(
        title=url,
        source=EBI,
        fetched_at=NOW,
        link=url,
        type="academic_paper",
    )


class FakeProvider:
    name = "fake"

    def __init__(self, results):
        self._results = results
        self.calls = []

    async def search(self, query, *, timeout=10.0):
        self.calls.append(query)
        return tuple(self._results.get(query.query, ()))


class FakeIntake:
    def __init__(self, outcomes=None):
        self._outcomes = dict(outcomes or {})
        self.calls = []

    async def fetch_source(self, url, *, kind="auto", process=True):
        self.calls.append(url)
        outcome = self._outcomes.get(url)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise AssertionError(f"unexpected intake URL {url!r}")
        return outcome


class FakeScholarly:
    """Resolver double over the ScholarlyMetadataClient.fetch() shape."""

    def __init__(self, links=None, error=None):
        self._links = dict(links or {})
        self._error = error
        self.calls = []

    async def fetch(self, value):
        self.calls.append(value)
        if self._error is not None:
            raise self._error
        return SourceItem(
            title="Canonical article",
            source="academic",
            fetched_at=NOW,
            link=self._links[value],
            type="academic",
        )


def intake_report(url):
    return SourceFetchReport(
        url=url,
        fetched_at=NOW,
        items=(
            SourceItemReport(
                title="Article",
                source="academic",
                link=url,
                material_id="mat-article",
                spool_path=Path("raw/spool/a.md"),
                raw_path=Path("raw/a.md"),
                corpus_path=Path("corpus/a.md"),
            ),
        ),
    )


def execute_plan(executor, plan):
    return asyncio.run(executor.execute(plan, process=False))


# --- detection: only the approved scholarly API endpoints -------------------


def test_ext_id_search_endpoint_is_detected_as_a_pmid_literal():
    assert scholarly_api_identifier(REST_SEARCH_EXT_ID) == "PMID:41349773"


def test_pmcid_search_endpoint_is_detected_as_a_pmcid_literal():
    assert scholarly_api_identifier(REST_SEARCH_PMCID) == "PMCID:PMC11577200"


def test_fulltext_xml_endpoint_is_detected_as_a_pmcid_literal():
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/"
        "PMC8879992/fullTextXML"
    )
    assert scholarly_api_identifier(url) == "PMCID:PMC8879992"


def test_identifiers_are_never_read_from_foreign_urls():
    for url in (
        "https://evil.example/rest/search?query=EXT_ID:41349773",
        "https://www.ebi.ac.uk.evil.example/search?query=EXT_ID:41349773",
        "https://www.ebi.ac.uk/other/search?query=EXT_ID:41349773",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=hello",
        "https://example.com/page?next=EXT_ID:41349773",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        "",
        "not a url",
    ):
        assert scholarly_api_identifier(url) is None, url


# --- executor: API leads resolve to the canonical article, never fetched ----


def test_ext_id_rest_lead_is_resolved_to_the_canonical_article():
    scholarly = FakeScholarly({"PMID:41349773": DOI_ARTICLE})
    intake = FakeIntake({DOI_ARTICLE: intake_report(DOI_ARTICLE)})
    executor = ResearchExecutor(
        FakeProvider({"article query": (lead(REST_SEARCH_EXT_ID),)}),
        intake,
        scholarly=scholarly,
    )

    report = execute_plan(executor, plan_for("article query"))

    assert scholarly.calls == ["PMID:41349773"]
    # The REST endpoint itself is never handed to the fetcher...
    assert intake.calls == [DOI_ARTICLE]
    execution = report.query_executions[0]
    assert [success.url for success in execution.successes] == [
        normalize_url(DOI_ARTICLE)
    ]
    assert execution.failures == ()
    assert report.material_ids == ("mat-article",)


def test_pmcid_rest_lead_is_resolved_to_the_pmc_article_page():
    scholarly = FakeScholarly({"PMCID:PMC11577200": PMC_ARTICLE})
    intake = FakeIntake({normalize_url(PMC_ARTICLE): intake_report(PMC_ARTICLE)})
    executor = ResearchExecutor(
        FakeProvider({"pmc query": (lead(REST_SEARCH_PMCID),)}),
        intake,
        scholarly=scholarly,
    )

    report = execute_plan(executor, plan_for("pmc query"))

    assert intake.calls == [normalize_url(PMC_ARTICLE)]
    assert report.query_executions[0].successes
    assert report.query_executions[0].failures == ()


def test_resolved_canonical_article_shares_the_dedup_with_a_direct_lead():
    # The same article reached both as a direct DOI lead and as a REST lead:
    # one authoritative fetch, the second is a recorded duplicate.
    scholarly = FakeScholarly({"PMID:41349773": DOI_ARTICLE})
    intake = FakeIntake({DOI_ARTICLE: intake_report(DOI_ARTICLE)})
    candidates = (
        SourceCandidate(
            domain=EBI,
            source_types=("academic_paper",),
            priority=1,
            reason="Whitelisted scholarly host.",
        ),
        SourceCandidate(
            domain="doi.org",
            source_types=("academic_paper",),
            priority=2,
            reason="Whitelisted DOI host.",
        ),
    )
    plan = plan_for(
        "mixed query",
        source_domains=(EBI, "doi.org"),
        candidates=candidates,
    )
    provider = FakeProvider(
        {"mixed query": (lead(DOI_ARTICLE), lead(REST_SEARCH_EXT_ID))}
    )
    executor = ResearchExecutor(provider, intake, scholarly=scholarly)

    report = execute_plan(executor, plan)

    assert intake.calls == [DOI_ARTICLE]
    execution = report.query_executions[0]
    assert len(execution.successes) == 1
    assert execution.duplicates == (normalize_url(DOI_ARTICLE),)


def test_unresolvable_rest_lead_is_dropped_with_an_auditable_failure():
    scholarly = FakeScholarly(
        error=SourceFetchError(
            FailureKind.PARSE,
            REST_SEARCH_EXT_ID,
            "no Europe PMC record matched the identifier",
        )
    )
    intake = FakeIntake()
    executor = ResearchExecutor(
        FakeProvider({"article query": (lead(REST_SEARCH_EXT_ID),)}),
        intake,
        scholarly=scholarly,
    )

    report = execute_plan(executor, plan_for("article query"))

    assert intake.calls == []
    execution = report.query_executions[0]
    assert execution.successes == ()
    failure = execution.failures[0]
    assert failure.kind == CANDIDATE_UNRESOLVED_LEAD == "unresolved_lead"
    assert failure.url == normalize_url(REST_SEARCH_EXT_ID)
    assert "PMID:41349773" in failure.detail
    assert "no Europe PMC record matched" in failure.detail


def test_rest_lead_without_a_scholarly_adapter_is_dropped_not_fetched():
    intake = FakeIntake()
    executor = ResearchExecutor(
        FakeProvider({"article query": (lead(REST_SEARCH_EXT_ID),)}),
        intake,
    )

    report = execute_plan(executor, plan_for("article query"))

    assert intake.calls == []
    failure = report.query_executions[0].failures[0]
    assert failure.kind == "unresolved_lead"
    assert "no scholarly adapter" in failure.detail


def test_direct_article_leads_are_unaffected_by_the_resolver():
    scholarly = FakeScholarly()
    intake = FakeIntake({DOI_ARTICLE: intake_report(DOI_ARTICLE)})
    executor = ResearchExecutor(
        FakeProvider({"doi query": (lead(DOI_ARTICLE),)}),
        intake,
        scholarly=scholarly,
    )

    report = execute_plan(
        executor,
        plan_for(
            "doi query",
            source_domains=("doi.org",),
            candidates=(
                SourceCandidate(
                    domain="doi.org",
                    source_types=("academic_paper",),
                    priority=1,
                    reason="Whitelisted DOI host.",
                ),
            ),
        ),
    )

    assert scholarly.calls == []
    assert intake.calls == [DOI_ARTICLE]
    assert report.query_executions[0].failures == ()


def test_executor_rejects_a_scholarly_seam_without_fetch():
    with pytest.raises(TypeError, match="scholarly"):
        ResearchExecutor(FakeProvider({}), FakeIntake(), scholarly=object())


# --- facade wiring: execute_research resolves through the injected client ----


class _StubIngestion:
    def ingest(self, path, metadata=None):
        raise AssertionError("not used")


class _StubStore:
    def index_file(self, path):
        return type("Outcome", (), {"status": "unchanged"})()

    def search(self, criteria, *, limit, offset):
        return []


class _StubGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("not used")

    async def add_case(self, case, **kwargs):
        raise AssertionError("not used")


class _StubEvents:
    async def publish(self, event):
        raise AssertionError("not used")


def test_facade_execute_research_resolves_rest_leads_through_scholarly_client():
    scholarly = FakeScholarly({"PMID:41349773": DOI_ARTICLE})
    intake = FakeIntake({DOI_ARTICLE: intake_report(DOI_ARTICLE)})
    api = PrismAPI(
        _StubIngestion(),
        _StubStore(),
        _StubGraph(),
        _StubEvents(),
        search_provider=FakeProvider(
            {"article query": (lead(REST_SEARCH_EXT_ID),)}
        ),
        research_intake=intake,
        scholarly_metadata_client=scholarly,
    )

    report = asyncio.run(
        api.execute_research(plan_for("article query"), process=False)
    )

    assert scholarly.calls == ["PMID:41349773"]
    assert intake.calls == [DOI_ARTICLE]
    assert report.query_executions[0].successes
