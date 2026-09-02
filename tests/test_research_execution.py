"""Tests for the PRISM ResearchPlan executor (evidence-discovery execution)."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from prism.api.fetching import SourceFetchReport, SourceItemReport
from prism.research import (
    CANDIDATE_DOMAIN_OUT_OF_SCOPE,
    CANDIDATE_INVALID_LEAD,
    CANDIDATE_NO_CONTENT,
    CANDIDATE_NO_LINK,
    PLAN_ORIGIN_FALLBACK,
    CandidateFailure,
    CandidateSuccess,
    FirecrawlTransportError,
    QueryExecution,
    ResearchConcept,
    ResearchExecutionReport,
    ResearchExecutor,
    ResearchPlan,
    ResearchWindow,
    SearchProvider,
    SearchQuery,
    SourceCandidate,
    SourceIntake,
)
from prism.sources import FailureKind, SourceFetchError, SourceItem

UTC = timezone.utc
PUBLISHED = datetime(2025, 6, 1, 8, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
PLANNED = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
EXECUTED = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
GOV = "gov.example"
NEWS = "news.example"

GOV_POLICY = "https://gov.example/policy"
NEWS_STORY = "https://news.example/story"


# --------------------------------------------------------------------------
# Shared fixtures / fakes
# --------------------------------------------------------------------------


def window(phase="publication", start=None, end=None, focus="Find the record."):
    return ResearchWindow(
        phase=phase,
        start_at=start or datetime(2025, 6, 1, tzinfo=UTC),
        end_at=end or datetime(2025, 9, 1, tzinfo=UTC),
        focus=focus,
    )


def candidate(domain=GOV, source_types=("policy_document",), priority=1):
    return SourceCandidate(
        domain=domain,
        source_types=source_types,
        priority=priority,
        reason="Primary policy record.",
    )


def query(
    text="data retention policy",
    win=None,
    source_domains=(GOV,),
    source_types=("policy_document",),
    reason="Locate the original text.",
):
    return SearchQuery(
        query=text,
        window=win or window(),
        source_domains=source_domains,
        source_types=source_types,
        reason=reason,
    )


def make_plan(**overrides):
    win = window()
    values = {
        "source_id": "material-7",
        "anchor_at": PUBLISHED,
        "frontier_at": FETCHED,
        "planned_at": PLANNED,
        "origin": PLAN_ORIGIN_FALLBACK,
        "case_tags": ("case-42",),
        "windows": (win,),
        "candidates": (candidate(GOV), candidate(NEWS, ("news",), 2)),
        "queries": (
            query(text="gov policy record", win=win, source_domains=(GOV,)),
            query(
                text="news policy coverage",
                win=win,
                source_domains=(GOV, NEWS),
                source_types=("news",),
            ),
        ),
    }
    values.update(overrides)
    return ResearchPlan(**values)


def lead(url, *, title="Discovery lead", content="# leaked discovery markdown", summary=None):
    host = (urlsplit(url).hostname or "").lower().rstrip(".") if url else GOV
    return SourceItem(
        title=title,
        source=host,
        fetched_at=EXECUTED,
        link=url,
        summary=summary,
        content=content,
        type="news",
    )


def intake_report(url, tmp_path, material_id="mat-0001"):
    item = SourceItemReport(
        title="Policy update",
        source=(urlsplit(url).hostname or "").lower().rstrip("."),
        link=url,
        material_id=material_id,
        spool_path=tmp_path / "spool" / f"source-{material_id}.md",
        raw_path=tmp_path / "raw" / f"{material_id}.md",
        corpus_path=tmp_path / "corpus" / f"{material_id}.md",
    )
    return SourceFetchReport(url=url, fetched_at=EXECUTED, items=(item,))


class FakeProvider:
    """Search seam double: canned per-query-text leads or exceptions."""

    name = "fake"

    def __init__(self, results=None):
        self._results = dict(results or {})
        self.calls = []

    async def search(self, query, *, timeout=10.0):
        self.calls.append((query.query, timeout))
        outcome = self._results.get(query.query, ())
        if isinstance(outcome, Exception):
            raise outcome
        return tuple(outcome)


class FakeIntake:
    """SourceIntake double: canned per-URL reports or exceptions."""

    def __init__(self, outcomes=None):
        self._outcomes = dict(outcomes or {})
        self.calls = []

    async def fetch_source(self, url, *, kind="auto", process=True):
        self.calls.append((url, kind, process))
        outcome = self._outcomes.get(url)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise AssertionError(f"unexpected intake URL {url!r}")
        return outcome


def make_executor(provider=None, intake=None, **overrides):
    return ResearchExecutor(
        provider or FakeProvider(),
        intake or FakeIntake(),
        clock=lambda: EXECUTED,
        **overrides,
    )


# --------------------------------------------------------------------------
# Construction and seam validation
# --------------------------------------------------------------------------


def test_source_intake_is_a_structural_seam():
    assert isinstance(FakeIntake(), SourceIntake)
    assert isinstance(FakeProvider(), SearchProvider)
    assert not isinstance(object(), SourceIntake)


def test_executor_rejects_bad_constructor_arguments():
    good_provider = FakeProvider()
    good_intake = FakeIntake()
    with pytest.raises(TypeError, match="provider"):
        ResearchExecutor(object(), good_intake)
    with pytest.raises(TypeError, match="intake"):
        ResearchExecutor(good_provider, object())
    for bad_max in (0, -1, True, "3", 2.5):
        with pytest.raises((TypeError, ValueError)):
            ResearchExecutor(good_provider, good_intake, max_candidates_per_query=bad_max)
    for bad_timeout in (0, -1, True, "fast"):
        with pytest.raises((TypeError, ValueError)):
            ResearchExecutor(good_provider, good_intake, search_timeout=bad_timeout)
    with pytest.raises(ValueError, match="kind"):
        ResearchExecutor(good_provider, good_intake, kind="  ")
    with pytest.raises(TypeError, match="clock"):
        ResearchExecutor(good_provider, good_intake, clock="now")


def test_executor_rejects_bad_execute_arguments():
    executor = make_executor()
    with pytest.raises(TypeError, match="plan"):
        asyncio.run(executor.execute("plan-of-action"))
    with pytest.raises(TypeError, match="process"):
        asyncio.run(executor.execute(make_plan(), process="yes"))


def test_executor_forwards_search_timeout_and_intake_kind(tmp_path):
    provider = FakeProvider({"gov policy record": (lead(GOV_POLICY),)})
    intake = FakeIntake({GOV_POLICY: intake_report(GOV_POLICY, tmp_path)})
    executor = make_executor(provider, intake, search_timeout=2.5, kind="article")
    asyncio.run(executor.execute(make_plan()))
    assert provider.calls[0] == ("gov policy record", 2.5)
    assert intake.calls[0][1] == "article"


# --------------------------------------------------------------------------
# Contract hygiene (frozen / slots / validation)
# --------------------------------------------------------------------------


def test_failure_kind_constants_are_pinned():
    assert CANDIDATE_NO_LINK == "no_link"
    assert CANDIDATE_DOMAIN_OUT_OF_SCOPE == "domain_out_of_scope"
    assert CANDIDATE_NO_CONTENT == "no_content"
    assert CANDIDATE_INVALID_LEAD == "invalid_lead"


def test_contracts_are_frozen_and_slotted(tmp_path):
    report = ResearchExecutionReport(
        source_id="material-7",
        case_tags=("case-42",),
        planned_at=PLANNED,
        executed_at=EXECUTED,
        process=True,
        query_executions=(),
    )
    execution = QueryExecution(
        query="q",
        window=window(),
        reason="r",
        source_domains=(GOV,),
        discovered=0,
    )
    success = CandidateSuccess(
        url=GOV_POLICY,
        material_ids=("mat-0001",),
        report=intake_report(GOV_POLICY, tmp_path),
    )
    failure = CandidateFailure(url=GOV_POLICY, kind=CANDIDATE_NO_CONTENT, detail="empty")
    for obj, attr, value in (
        (report, "source_id", "other"),
        (execution, "query", "other"),
        (success, "url", "https://other.example/"),
        (failure, "kind", "no_link"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, value)
    for obj in (report, execution, success, failure):
        assert not hasattr(obj, "__dict__")


def test_contracts_reject_bad_payloads(tmp_path):
    good_report = intake_report(GOV_POLICY, tmp_path)
    with pytest.raises(ValueError, match="source_id"):
        ResearchExecutionReport(
            source_id=" ",
            case_tags=(),
            planned_at=PLANNED,
            executed_at=EXECUTED,
            process=True,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        ResearchExecutionReport(
            source_id="m",
            case_tags=(),
            planned_at=PLANNED,
            executed_at=datetime(2026, 9, 1, 8, 0),
            process=True,
        )
    with pytest.raises(TypeError, match="process"):
        ResearchExecutionReport(
            source_id="m",
            case_tags=(),
            planned_at=PLANNED,
            executed_at=EXECUTED,
            process="true",
        )
    with pytest.raises(TypeError, match="QueryExecution"):
        ResearchExecutionReport(
            source_id="m",
            case_tags=(),
            planned_at=PLANNED,
            executed_at=EXECUTED,
            process=True,
            query_executions=("query",),
        )
    with pytest.raises(TypeError, match="window"):
        QueryExecution(
            query="q", window="publication", reason="r",
            source_domains=(GOV,), discovered=0,
        )
    with pytest.raises(ValueError, match="query"):
        QueryExecution(
            query=" ", window=window(), reason="r",
            source_domains=(GOV,), discovered=0,
        )
    with pytest.raises(ValueError, match="discovered"):
        QueryExecution(
            query="q", window=window(), reason="r",
            source_domains=(GOV,), discovered=-1,
        )
    with pytest.raises(TypeError, match="discovered"):
        QueryExecution(
            query="q", window=window(), reason="r",
            source_domains=(GOV,), discovered=True,
        )
    with pytest.raises(TypeError, match="CandidateSuccess"):
        QueryExecution(
            query="q", window=window(), reason="r",
            source_domains=(GOV,), discovered=0, successes=("win",),
        )
    with pytest.raises(ValueError, match="provider_error"):
        QueryExecution(
            query="q", window=window(), reason="r",
            source_domains=(GOV,), discovered=0, provider_error=42,
        )
    with pytest.raises(ValueError, match="url"):
        CandidateFailure(url=" ", kind=CANDIDATE_NO_LINK, detail="d")
    with pytest.raises(ValueError, match="kind"):
        CandidateFailure(url=GOV_POLICY, kind=" ", detail="d")
    with pytest.raises(ValueError, match="detail"):
        CandidateFailure(url=GOV_POLICY, kind=CANDIDATE_NO_LINK, detail=" ")
    with pytest.raises(ValueError, match="url"):
        CandidateSuccess(url=" ", material_ids=("m",), report=good_report)
    with pytest.raises(ValueError, match="material_ids"):
        CandidateSuccess(url=GOV_POLICY, material_ids=(" ",), report=good_report)
    with pytest.raises(TypeError, match="SourceFetchReport"):
        CandidateSuccess(url=GOV_POLICY, material_ids=("m",), report="fetched")


# --------------------------------------------------------------------------
# Happy path: discovery leads through the authoritative intake
# --------------------------------------------------------------------------


def test_execute_runs_queries_in_plan_order_and_records_successes(tmp_path):
    gov_item = intake_report(GOV_POLICY, tmp_path, "mat-gov")
    news_item = intake_report(NEWS_STORY, tmp_path, "mat-news")
    provider = FakeProvider(
        {
            "gov policy record": (lead(GOV_POLICY),),
            "news policy coverage": (lead(NEWS_STORY),),
        }
    )
    intake = FakeIntake({GOV_POLICY: gov_item, NEWS_STORY: news_item})
    executor = make_executor(provider, intake)
    plan = make_plan()

    async def exercise():
        original = asyncio.create_task
        def forbidden(*args, **kwargs):
            raise AssertionError("executor must not create background tasks")
        asyncio.create_task = forbidden
        try:
            return await executor.execute(plan)
        finally:
            asyncio.create_task = original

    report = asyncio.run(exercise())

    assert isinstance(report, ResearchExecutionReport)
    assert report.source_id == "material-7"
    assert report.case_tags == ("case-42",)
    assert report.planned_at == PLANNED
    assert report.executed_at == EXECUTED
    assert report.process is True
    assert [e.query for e in report.query_executions] == [
        "gov policy record",
        "news policy coverage",
    ]
    gov_execution, news_execution = report.query_executions
    assert gov_execution.provider_error is None
    assert gov_execution.window is plan.windows[0]
    assert gov_execution.reason == plan.queries[0].reason
    assert gov_execution.source_domains == (GOV,)
    assert gov_execution.discovered == 1
    assert [s.url for s in gov_execution.successes] == [GOV_POLICY]
    success = gov_execution.successes[0]
    assert success.material_ids == ("mat-gov",)
    assert success.report == gov_item
    assert success.report is not gov_item
    assert [s.url for s in news_execution.successes] == [NEWS_STORY]
    assert report.material_ids == ("mat-gov", "mat-news")
    assert [c[0] for c in provider.calls] == ["gov policy record", "news policy coverage"]
    assert provider.calls[0][1] == 10.0
    assert [c[:2] for c in intake.calls] == [(GOV_POLICY, "page"), (NEWS_STORY, "page")]
    assert all(c[2] is True for c in intake.calls)


def test_discovery_payloads_are_never_treated_as_evidence(tmp_path):
    """Lead content must stay discovery; only links may be re-collected."""
    juicy = SourceItem(
        title="Mysterious memo",
        source=GOV,
        fetched_at=EXECUTED,
        link=None,
        summary="leaked summary",
        content="# full secret discovery body",
    )
    provider = FakeProvider({"gov policy record": (juicy, lead(GOV_POLICY))})
    gov_item = intake_report(GOV_POLICY, tmp_path, "mat-real")
    intake = FakeIntake({GOV_POLICY: gov_item})
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan(queries=(
        query(text="gov policy record", source_domains=(GOV,)),
    ))))

    execution = report.query_executions[0]
    assert [f.kind for f in execution.failures] == [CANDIDATE_NO_LINK]
    assert execution.failures[0].url is None
    assert [s.url for s in execution.successes] == [GOV_POLICY]
    assert execution.successes[0].material_ids == ("mat-real",)
    assert report.material_ids == ("mat-real",)
    dumped = repr(report)
    assert "secret discovery body" not in dumped
    assert "leaked summary" not in dumped


def test_out_of_scope_candidates_are_rejected_without_intake(tmp_path):
    off_scope = "https://papers.example/preprint"
    provider = FakeProvider(
        {"gov policy record": (lead(off_scope), lead(GOV_POLICY))}
    )
    intake = FakeIntake({GOV_POLICY: intake_report(GOV_POLICY, tmp_path)})
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan(queries=(
        query(text="gov policy record", source_domains=(GOV,)),
    ))))

    execution = report.query_executions[0]
    assert [f.kind for f in execution.failures] == [CANDIDATE_DOMAIN_OUT_OF_SCOPE]
    assert execution.failures[0].url == off_scope
    assert "papers.example" in execution.failures[0].detail
    assert [s.url for s in execution.successes] == [GOV_POLICY]
    assert [c[0] for c in intake.calls] == [GOV_POLICY]


def test_invalid_provider_entries_are_recorded_not_crashed(tmp_path):
    provider = FakeProvider({"gov policy record": ("https://gov.example/x", lead(GOV_POLICY))})
    intake = FakeIntake({GOV_POLICY: intake_report(GOV_POLICY, tmp_path)})
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan(queries=(
        query(text="gov policy record", source_domains=(GOV,)),
    ))))

    execution = report.query_executions[0]
    assert [f.kind for f in execution.failures] == [CANDIDATE_INVALID_LEAD]
    assert [s.url for s in execution.successes] == [GOV_POLICY]


def test_urls_are_deduplicated_across_queries_by_normalized_link(tmp_path):
    gov_item = intake_report(GOV_POLICY, tmp_path, "mat-gov")
    other = "https://gov.example/other"
    other_item = intake_report(other, tmp_path, "mat-other")
    provider = FakeProvider(
        {
            "gov policy record": (
                lead(GOV_POLICY),
                lead(GOV_POLICY),  # duplicate inside one query
            ),
            "news policy coverage": (
                lead("https://GOV.example/policy/"),  # same URL, other casing/slash
                lead(other),
            ),
        }
    )
    intake = FakeIntake({GOV_POLICY: gov_item, other: other_item})
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan()))

    gov_execution, news_execution = report.query_executions
    assert [s.url for s in gov_execution.successes] == [GOV_POLICY]
    assert gov_execution.duplicates == (GOV_POLICY,)  # repeat inside one query
    assert news_execution.duplicates == (GOV_POLICY,)  # repeat across queries
    assert [s.url for s in news_execution.successes] == [other]
    assert [c[0] for c in intake.calls] == [GOV_POLICY, other]
    assert report.material_ids == ("mat-gov", "mat-other")


def test_single_candidate_failure_does_not_stop_other_candidates(tmp_path):
    blocked = "https://gov.example/blocked"
    empty = "https://gov.example/empty"
    provider = FakeProvider(
        {"gov policy record": (lead(blocked), lead(empty), lead(GOV_POLICY))}
    )
    intake = FakeIntake(
        {
            blocked: SourceFetchError(
                FailureKind.BLOCKED, blocked, "URL violates the SSRF policy"
            ),
            empty: SourceFetchReport(url=empty, fetched_at=EXECUTED, items=()),
            GOV_POLICY: intake_report(GOV_POLICY, tmp_path, "mat-gov"),
        }
    )
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan(queries=(
        query(text="gov policy record", source_domains=(GOV,)),
    ))))

    execution = report.query_executions[0]
    assert [(f.url, f.kind) for f in execution.failures] == [
        (blocked, "blocked"),
        (empty, CANDIDATE_NO_CONTENT),
    ]
    assert "SSRF" in execution.failures[0].detail
    assert [s.url for s in execution.successes] == [GOV_POLICY]
    assert len(intake.calls) == 3


def test_generic_intake_exception_is_classified_by_name(tmp_path):
    provider = FakeProvider({"gov policy record": (lead(GOV_POLICY),)})
    intake = FakeIntake(
        {GOV_POLICY: RuntimeError("pipeline down mid-ingestion")}
    )
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan(queries=(
        query(text="gov policy record", source_domains=(GOV,)),
    ))))

    execution = report.query_executions[0]
    assert [f.kind for f in execution.failures] == ["RuntimeError"]
    assert "pipeline down" in execution.failures[0].detail
    assert execution.successes == ()
    assert report.material_ids == ()


def test_execution_audit_redacts_provider_and_intake_secret_messages(tmp_path):
    provider = FakeProvider(
        {"gov policy record": RuntimeError("Authorization: Bearer provider-secret")}
    )
    provider_executor = make_executor(provider, FakeIntake())

    provider_report = asyncio.run(
        provider_executor.execute(
            make_plan(queries=(query(text="gov policy record", source_domains=(GOV,)),))
        )
    )
    assert "provider-secret" not in provider_report.query_executions[0].provider_error
    assert "[REDACTED]" in provider_report.query_executions[0].provider_error

    intake = FakeIntake(
        {
            GOV_POLICY: SourceFetchError(
                FailureKind.TRANSPORT,
                GOV_POLICY,
                "Authorization: Bearer intake-secret",
            )
        }
    )
    intake_executor = make_executor(
        FakeProvider({"intake query": (lead(GOV_POLICY),)}), intake
    )
    intake_report_value = asyncio.run(
        intake_executor.execute(
            make_plan(queries=(query(text="intake query", source_domains=(GOV,)),))
        )
    )
    failure = intake_report_value.query_executions[0].failures[0]
    assert "intake-secret" not in failure.detail
    assert "[REDACTED]" in failure.detail


def test_execution_audit_redacts_candidate_url_credentials(tmp_path):
    token_url = "https://gov.example/policy?access_token=candidate-secret"
    intake = FakeIntake(
        {
            token_url: SourceFetchError(
                FailureKind.TRANSPORT,
                token_url,
                "network failed",
            )
        }
    )
    executor = make_executor(
        FakeProvider({"token query": (lead(token_url),)}), intake
    )

    report = asyncio.run(
        executor.execute(
            make_plan(queries=(query(text="token query", source_domains=(GOV,)),))
        )
    )

    failure = report.query_executions[0].failures[0]
    assert "candidate-secret" not in failure.url
    assert "[REDACTED]" in failure.url
    assert intake.calls[0][0] == token_url


def test_success_audit_report_redacts_nested_intake_urls(tmp_path):
    token_url = "https://gov.example/policy?refresh_token=success-secret"
    original_report = intake_report(token_url, tmp_path, "mat-success")
    intake = FakeIntake({token_url: original_report})
    executor = make_executor(
        FakeProvider({"success query": (lead(token_url),)}), intake
    )

    execution_report = asyncio.run(
        executor.execute(
            make_plan(queries=(query(text="success query", source_domains=(GOV,)),))
        )
    )

    success = execution_report.query_executions[0].successes[0]
    assert intake.calls[0][0] == token_url
    assert "success-secret" not in success.url
    assert "success-secret" not in success.report.url
    assert "success-secret" not in success.report.items[0].link
    assert "success-secret" not in repr(execution_report)


def test_provider_exception_is_isolated_per_query(tmp_path):
    news_item = intake_report(NEWS_STORY, tmp_path, "mat-news")
    provider = FakeProvider(
        {
            "gov policy record": FirecrawlTransportError("search backend offline"),
            "news policy coverage": (lead(NEWS_STORY),),
        }
    )
    intake = FakeIntake({NEWS_STORY: news_item})
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan()))

    gov_execution, news_execution = report.query_executions
    assert gov_execution.provider_error is not None
    assert gov_execution.provider_error.startswith("FirecrawlTransportError")
    assert gov_execution.successes == ()
    assert gov_execution.failures == ()
    assert gov_execution.discovered == 0
    assert news_execution.provider_error is None
    assert [s.url for s in news_execution.successes] == [NEWS_STORY]
    assert len(provider.calls) == 2
    assert [c[0] for c in intake.calls] == [NEWS_STORY]


def test_max_candidates_per_query_limits_intake_attempts(tmp_path):
    first = "https://gov.example/first"
    second = "https://gov.example/second"
    third = "https://gov.example/third"
    outcomes = {
        url: intake_report(url, tmp_path, f"mat-{name}")
        for url, name in ((first, "a"), (second, "b"), (third, "c"))
    }
    provider = FakeProvider(
        {"gov policy record": (lead(first), lead(second), lead(third))}
    )
    intake = FakeIntake(outcomes)
    executor = make_executor(provider, intake, max_candidates_per_query=2)

    report = asyncio.run(executor.execute(make_plan(queries=(
        query(text="gov policy record", source_domains=(GOV,)),
    ))))

    execution = report.query_executions[0]
    assert [c[0] for c in intake.calls] == [first, second]
    assert [s.url for s in execution.successes] == [first, second]
    assert execution.failures == ()
    assert report.material_ids == ("mat-a", "mat-b")


def test_concept_query_uses_result_limit_as_candidate_budget(tmp_path):
    concept = ResearchConcept(
        "policy", "Policy", "policy records", (), ("material-7",), 10
    )
    urls = [f"https://gov.example/item-{i}" for i in range(12)]
    outcomes = {url: intake_report(url, tmp_path, f"mat-{i}") for i, url in enumerate(urls)}
    provider = FakeProvider({"gov policy record": tuple(lead(url) for url in urls)})
    intake = FakeIntake(outcomes)
    executor = make_executor(provider, intake, max_candidates_per_query=2)
    plan = make_plan(
        concepts=(concept,),
        queries=(
            SearchQuery(
                "gov policy record", window(), ("news",), (GOV,), "concept search",
                concept_id="policy", result_limit=10,
            ),
        ),
    )

    report = asyncio.run(executor.execute(plan))

    execution = report.query_executions[0]
    assert execution.concept_id == "policy"
    assert len(intake.calls) == 10
    assert len(execution.successes) == 10
    assert execution.discovered == 12
def test_timeout_search_is_retried_once_and_then_collected(tmp_path):
    class FlakyProvider:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        async def search(self, query, *, timeout=10.0):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary timeout")
            return (lead(GOV_POLICY),)

    provider = FlakyProvider()
    intake = FakeIntake({GOV_POLICY: intake_report(GOV_POLICY, tmp_path)})
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan()))

    assert provider.calls == 3
    assert report.query_executions[0].provider_error is None
    assert report.query_executions[0].successes
def test_concept_budget_is_shared_across_multiple_queries(tmp_path):
    concept = ResearchConcept(
        "policy", "Policy", "policy records", (), ("material-7",), 10
    )
    urls = [f"https://gov.example/shared-{i}" for i in range(12)]
    outcomes = {url: intake_report(url, tmp_path, f"mat-{i}") for i, url in enumerate(urls)}
    provider = FakeProvider(
        {
            "gov policy first": tuple(lead(url) for url in urls[:7]),
            "gov policy second": tuple(lead(url) for url in urls[5:]),
        }
    )
    intake = FakeIntake(outcomes)
    executor = make_executor(provider, intake, max_candidates_per_query=20)
    plan = make_plan(
        concepts=(concept,),
        queries=(
            SearchQuery(
                "gov policy first", window(), ("news",), (GOV,), "first",
                concept_id="policy", result_limit=10,
            ),
            SearchQuery(
                "gov policy second", window(), ("news",), (GOV,), "second",
                concept_id="policy", result_limit=10,
            ),
        ),
    )

    report = asyncio.run(executor.execute(plan))

    assert len(intake.calls) == 10
    assert sum(len(q.successes) for q in report.query_executions) == 10
def test_empty_plan_makes_no_requests():
    provider = FakeProvider()
    intake = FakeIntake()
    executor = make_executor(provider, intake)

    report = asyncio.run(executor.execute(make_plan(queries=(), windows=(window(),))))

    assert report.query_executions == ()
    assert report.material_ids == ()
    assert provider.calls == []
    assert intake.calls == []


def test_process_false_is_forwarded_and_recorded(tmp_path):
    provider = FakeProvider({"gov policy record": (lead(GOV_POLICY),)})
    intake = FakeIntake({GOV_POLICY: intake_report(GOV_POLICY, tmp_path)})
    executor = make_executor(provider, intake)

    report = asyncio.run(
        executor.execute(
            make_plan(queries=(query(text="gov policy record", source_domains=(GOV,)),)),
            process=False,
        )
    )

    assert report.process is False
    assert all(c[2] is False for c in intake.calls)
    assert report.query_executions[0].successes
