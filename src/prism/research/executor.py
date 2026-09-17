"""Execution of PRISM research plans: discovery leads to re-collected evidence.

``ResearchExecutor`` runs one :class:`~prism.research.models.ResearchPlan`
query by query in the plan's canonical order.  Every
:class:`~prism.research.models.SearchQuery` is handed to the injected
:class:`~prism.research.provider.SearchProvider`, whose
:class:`~prism.sources.SourceItem` results are *discovery leads only*: the
executor reads nothing but each lead's public ``link`` — scraped markdown,
summaries, and titles are never promoted to evidence.  Each surviving
candidate URL is then fetched a second time through the injected
:class:`SourceIntake` seam (satisfied by
:meth:`prism.api.PrismAPI.fetch_source`), which performs the authoritative
whitelist/SSRF-validated page fetch, raw spooling, corpus ingestion, and
optional pipeline run.  Nothing here fetches, parses, or ingests on its own,
and no background tasks are created: every await is sequential.

Outcome accounting is exhaustive and classified.  Per query the executor
records successes (candidate URL plus the verbatim intake report and its
material ids) and :class:`CandidateFailure` entries — leads without a usable
link, leads whose host falls outside the query's ``source_domains``, scholarly
API endpoints that could not be resolved to a canonical article, intake
failures classified by ``FailureKind`` value or exception class name, and
intakes that returned no collectible items (a URL with no real body is never
dressed up as evidence).  A discovery lead that *is* a scholarly API endpoint
(a Europe PMC REST search or fullTextXML URL) is resolved to the canonical
article URL through the injected
:class:`ScholarlyLeadResolver` — the authoritative Europe PMC / PMC path — or
dropped with an ``unresolved_lead`` audit record; the API URL itself is never
handed to the fetcher as if it were an article.  URLs are deduplicated by
normalized link across queries, one authoritative fetch attempt per URL per
execution, and skips are recorded per query for audit.  A single failing
candidate or provider never
aborts the plan; the resulting :class:`ResearchExecutionReport` preserves the
plan's ``source_id``, ``case_tags``, and planning time so every intake report
stays traceable back to the query, window, and reason that produced it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
import re
from urllib.parse import urlsplit

from prism.api.fetching import SourceFetchReport, SourceItemReport
from prism.sources import SourceFetchError, SourceItem, normalize_url

from .leads import scholarly_api_identifier
from .models import (
    PLAN_ORIGINS,
    PLAN_ORIGIN_FALLBACK,
    ResearchPlan,
    ResearchWindow,
    SearchQuery,
)
from .provider import SearchProvider

CANDIDATE_NO_LINK = "no_link"
CANDIDATE_INVALID_LEAD = "invalid_lead"
CANDIDATE_INVALID_INTAKE = "invalid_intake"
CANDIDATE_DOMAIN_OUT_OF_SCOPE = "domain_out_of_scope"
CANDIDATE_NO_CONTENT = "no_content"
# A scholarly *API* endpoint lead (e.g. a Europe PMC REST search URL) that
# could not be resolved to the canonical article URL: dropped for audit, and
# never handed to the fetcher as if it were an article.
CANDIDATE_UNRESOLVED_LEAD = "unresolved_lead"

DEFAULT_MAX_CANDIDATES_PER_QUERY = 3
DEFAULT_SEARCH_TIMEOUT = 10.0
DEFAULT_SEARCH_RETRIES = 1
DEFAULT_INTAKE_KIND = "page"

_SENSITIVE_VALUE = re.compile(
    r"(?i)((?<![A-Za-z0-9_-])(?:access[_-]?token|refresh[_-]?token|"
    r"api[_-]?(?:key|token)|authorization|credential|password|passwd|"
    r"secret|token|key|x-amz-signature)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")


def _safe_text(value: str) -> str:
    """Redact credential-looking fragments in arbitrary audit text."""
    value = _BEARER_VALUE.sub(r"\1[REDACTED]", value)
    return _SENSITIVE_VALUE.sub(r"\1[REDACTED]", value)


def _safe_error_detail(error: BaseException) -> str:
    """Retain error class/reason for audit without preserving credentials."""
    return _safe_text(str(error))


def _safe_url(url: str) -> str:
    """Render a candidate URL for audit without exposing credential query data."""
    return _safe_text(url)


def _safe_fetch_report(report: SourceFetchReport) -> SourceFetchReport:
    """Copy an intake report for audit without retaining credential-bearing URLs."""
    items = tuple(
        SourceItemReport(
            title=item.title,
            source=item.source,
            link=_safe_url(item.link) if item.link is not None else None,
            material_id=item.material_id,
            spool_path=item.spool_path,
            raw_path=item.raw_path,
            corpus_path=item.corpus_path,
            pipeline=item.pipeline,
            access_level=item.access_level,
        )
        for item in report.items
    )
    return SourceFetchReport(
        url=_safe_url(report.url),
        fetched_at=report.fetched_at,
        items=items,
        duplicate_keys=tuple(_safe_text(key) for key in report.duplicate_keys),
    )



def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_aware_datetime(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _text_tuple(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{name} must be an iterable of strings") from error
    for value in normalized:
        _require_text(name, value)
    return normalized


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""


@runtime_checkable
class SourceIntake(Protocol):
    """Authoritative second-fetch seam for candidate URLs.

    Satisfied structurally by :class:`prism.api.PrismAPI` via its
    ``fetch_source`` method: one whitelist/SSRF-validated public fetch of
    ``url`` followed by raw spooling, corpus ingestion through the
    :class:`~prism.ingestion.IngestionService`, an optional pipeline run,
    and an event — never a shortcut around them.
    """

    async def fetch_source(
        self, url: str, *, kind: str = ..., process: bool = ...
    ) -> SourceFetchReport: ...


@runtime_checkable
class ScholarlyLeadResolver(Protocol):
    """Resolution seam for scholarly API discovery leads.

    Satisfied structurally by
    :class:`prism.sources.ScholarlyMetadataClient`: ``fetch`` takes an
    explicit ``PMID:``/``PMCID:`` literal and returns a record-like object
    (a :class:`~prism.sources.SourceItem`) whose ``link`` is the canonical
    article URL — the DOI landing page, PMC article page, or PubMed record —
    resolved through the authoritative scholarly path (Europe PMC / PMC).
    Access controls are never bypassed; the adapter uses public APIs only.
    """

    async def fetch(self, value: str) -> object: ...


@dataclass(frozen=True, slots=True)
class CandidateFailure:
    """One classified rejection or fetch failure for a discovery lead.

    ``url`` is the normalized candidate link, or ``None`` when the lead
    carried no link at all.  ``kind`` is one of the ``CANDIDATE_*`` values,
    a :class:`~prism.sources.FailureKind` value from a classified intake
    rejection, or the exception class name for unexpected intake errors.
    """

    url: str | None
    kind: str
    detail: str

    def __post_init__(self) -> None:
        if self.url is not None:
            _require_text("url", self.url)
        _require_text("kind", self.kind)
        _require_text("detail", self.detail)


@dataclass(frozen=True, slots=True)
class CandidateSuccess:
    """One candidate URL re-collected as evidence through the intake.

    ``report`` is the verbatim intake report — spool, raw, and corpus paths
    included — so the executor never invents its own evidence records, and
    ``material_ids`` mirrors exactly the materials the intake produced.
    """

    url: str
    material_ids: tuple[str, ...]
    report: SourceFetchReport

    def __post_init__(self) -> None:
        _require_text("url", self.url)
        object.__setattr__(
            self, "material_ids", _text_tuple("material_ids", self.material_ids)
        )
        if not isinstance(self.report, SourceFetchReport):
            raise TypeError("report must be a SourceFetchReport")


@dataclass(frozen=True, slots=True)
class QueryExecution:
    """The auditable outcome of executing one planned search query.

    Mirrors the query's text, window, reason, and source-domain scope, and
    counts how many discovery leads the provider returned.  ``duplicates``
    lists normalized URLs skipped because an earlier lead (in this query or
    a previous one) already claimed them; ``provider_error`` carries the
    classified search failure when the provider itself raised.
    """

    query: str
    window: ResearchWindow
    reason: str
    source_domains: tuple[str, ...]
    discovered: int
    concept_id: str | None = None
    successes: tuple[CandidateSuccess, ...] = ()
    failures: tuple[CandidateFailure, ...] = ()
    duplicates: tuple[str, ...] = ()
    provider_error: str | None = None

    def __post_init__(self) -> None:
        _require_text("query", self.query)
        if not isinstance(self.window, ResearchWindow):
            raise TypeError("window must be a ResearchWindow")
        _require_text("reason", self.reason)
        if self.concept_id is not None:
            _require_text("concept_id", self.concept_id)
        object.__setattr__(
            self, "source_domains", _text_tuple("source_domains", self.source_domains)
        )
        if isinstance(self.discovered, bool) or not isinstance(self.discovered, int):
            raise TypeError("discovered must be an integer")
        if self.discovered < 0:
            raise ValueError("discovered must not be negative")
        object.__setattr__(self, "successes", tuple(self.successes))
        for success in self.successes:
            if not isinstance(success, CandidateSuccess):
                raise TypeError("successes must contain only CandidateSuccess objects")
        object.__setattr__(self, "failures", tuple(self.failures))
        for failure in self.failures:
            if not isinstance(failure, CandidateFailure):
                raise TypeError("failures must contain only CandidateFailure objects")
        object.__setattr__(self, "duplicates", _text_tuple("duplicates", self.duplicates))
        if self.provider_error is not None:
            _require_text("provider_error", self.provider_error)


@dataclass(frozen=True, slots=True)
class ResearchExecutionReport:
    """The complete, ordered record of one research-plan execution.

    Preserves the plan's identity (``source_id``, ``case_tags``,
    ``planned_at``) alongside the per-query executions, so each candidate
    URL and intake report remains traceable to the query, window, and reason
    that surfaced it.  The plan provenance fields (``plan_origin``,
    ``plan_warnings``, ``plan_query_count``, ``plan_fingerprint``) carry the
    planning side into the artifact: a run executed on a degraded fallback
    plan stays visible in the result instead of masquerading as a good
    LLM-plan run (real-run finding 2026-09-16).
    """

    source_id: str
    case_tags: tuple[str, ...]
    planned_at: datetime
    executed_at: datetime
    process: bool
    query_executions: tuple[QueryExecution, ...] = ()
    plan_origin: str = PLAN_ORIGIN_FALLBACK
    plan_warnings: tuple[str, ...] = ()
    plan_query_count: int = 0
    plan_fingerprint: str = ""

    def __post_init__(self) -> None:
        _require_text("source_id", self.source_id)
        object.__setattr__(self, "case_tags", _text_tuple("case_tags", self.case_tags))
        _require_aware_datetime("planned_at", self.planned_at)
        _require_aware_datetime("executed_at", self.executed_at)
        if not isinstance(self.process, bool):
            raise TypeError("process must be a bool")
        object.__setattr__(
            self, "query_executions", tuple(self.query_executions)
        )
        for execution in self.query_executions:
            if not isinstance(execution, QueryExecution):
                raise TypeError(
                    "query_executions must contain only QueryExecution objects"
                )
        if self.plan_origin not in PLAN_ORIGINS:
            raise ValueError(
                f"plan_origin must be one of: {', '.join(sorted(PLAN_ORIGINS))}"
            )
        object.__setattr__(
            self, "plan_warnings", _text_tuple("plan_warnings", self.plan_warnings)
        )
        if (
            isinstance(self.plan_query_count, bool)
            or not isinstance(self.plan_query_count, int)
        ):
            raise TypeError("plan_query_count must be an integer")
        if self.plan_query_count < 0:
            raise ValueError("plan_query_count must not be negative")
        if not isinstance(self.plan_fingerprint, str):
            raise TypeError("plan_fingerprint must be a string")

    @property
    def material_ids(self) -> tuple[str, ...]:
        """Every collected material id, in collection order, without repeats."""
        seen: set[str] = set()
        ordered: list[str] = []
        for execution in self.query_executions:
            for success in execution.successes:
                for material_id in success.material_ids:
                    if material_id not in seen:
                        seen.add(material_id)
                        ordered.append(material_id)
        return tuple(ordered)


class ResearchExecutor:
    """Execute a research plan: search for leads, re-collect them as evidence.

    Dependencies are fully injected: a ``provider`` to run each planned
    query and an ``intake`` to authoritatively fetch each surviving
    candidate URL.  The executor contributes only policy and bookkeeping —
    link-only candidate extraction, per-query domain scoping, normalized-URL
    deduplication, the per-query candidate cap, classified failure records,
    and deterministic sequencing (plan order for queries, provider order for
    leads).  It owns no whitelist and no network client of its own.
    """

    def __init__(
        self,
        provider: SearchProvider,
        intake: SourceIntake,
        *,
        max_candidates_per_query: int = DEFAULT_MAX_CANDIDATES_PER_QUERY,
        search_timeout: float = DEFAULT_SEARCH_TIMEOUT,
        search_retries: int = DEFAULT_SEARCH_RETRIES,
        kind: str = DEFAULT_INTAKE_KIND,
        scholarly: ScholarlyLeadResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(provider, "search", None)):
            raise TypeError("provider must provide search()")
        if not callable(getattr(intake, "fetch_source", None)):
            raise TypeError("intake must provide fetch_source()")
        if scholarly is not None and not callable(
            getattr(scholarly, "fetch", None)
        ):
            raise TypeError("scholarly must provide fetch()")
        if (
            isinstance(max_candidates_per_query, bool)
            or not isinstance(max_candidates_per_query, int)
        ):
            raise TypeError("max_candidates_per_query must be an integer")
        if max_candidates_per_query < 1:
            raise ValueError("max_candidates_per_query must be at least 1")
        if isinstance(search_timeout, bool) or not isinstance(search_timeout, (int, float)):
            raise TypeError("search_timeout must be a number")
        if search_timeout <= 0:
            raise ValueError("search_timeout must be greater than zero")
        if isinstance(search_retries, bool) or not isinstance(search_retries, int):
            raise TypeError("search_retries must be an integer")
        if search_retries < 0:
            raise ValueError("search_retries must not be negative")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("kind must be a non-empty string")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._provider = provider
        self._intake = intake
        self._scholarly = scholarly
        self._max_candidates = max_candidates_per_query
        self._search_timeout = float(search_timeout)
        self._search_retries = search_retries
        self._kind = kind.strip()
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )

    async def execute(
        self, plan: ResearchPlan, *, process: bool = True
    ) -> ResearchExecutionReport:
        """Run every planned query and return the full execution record."""
        if not isinstance(plan, ResearchPlan):
            raise TypeError("plan must be a ResearchPlan")
        if not isinstance(process, bool):
            raise TypeError("process must be a bool")
        executed_at = self._now()
        # plan.queries is already in canonical deterministic order; the seen
        # set dedupes candidate URLs across queries by normalized link.
        seen: set[str] = set()
        concept_budgets = {
            concept.concept_id: concept.target_results for concept in plan.concepts
        }
        concept_attempted: dict[str, int] = {}
        executions = [
            await self._execute_query(
                query,
                seen,
                concept_budgets,
                concept_attempted,
                process=process,
            )
            for query in plan.queries
        ]
        return ResearchExecutionReport(
            source_id=plan.source_id,
            case_tags=plan.case_tags,
            planned_at=plan.planned_at,
            executed_at=executed_at,
            process=process,
            query_executions=tuple(executions),
            plan_origin=plan.origin,
            plan_warnings=plan.warnings,
            plan_query_count=len(plan.queries),
            plan_fingerprint=plan.fingerprint(),
        )

    async def _execute_query(
        self,
        query: SearchQuery,
        seen: set[str],
        concept_budgets: dict[str, int],
        concept_attempted: dict[str, int],
        *,
        process: bool,
    ) -> QueryExecution:
        provider_error: str | None = None
        leads: tuple | None = None
        provider_exception: Exception | None = None
        for attempt in range(self._search_retries + 1):
            try:
                leads = tuple(
                    await self._provider.search(query, timeout=self._search_timeout)
                )
                provider_exception = None
                break
            except Exception as error:
                provider_exception = error
                is_timeout = isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower()
                if not is_timeout or attempt >= self._search_retries:
                    break
        if provider_exception is not None:
            provider_error = (
                f"{type(provider_exception).__name__}: "
                f"{_safe_error_detail(provider_exception)}"
            )
            leads = None

        successes: list[CandidateSuccess] = []
        failures: list[CandidateFailure] = []
        duplicates: list[str] = []
        attempted = 0
        max_candidates = (
            min(
                query.result_limit,
                max(0, concept_budgets[query.concept_id] - concept_attempted.get(query.concept_id, 0)),
            )
            if query.concept_id in concept_budgets
            else self._max_candidates
        )
        if leads is not None:
            for index, entry in enumerate(leads):
                if attempted >= max_candidates:
                    break
                if not isinstance(entry, SourceItem):
                    failures.append(
                        CandidateFailure(
                            url=None,
                            kind=CANDIDATE_INVALID_LEAD,
                            detail=f"provider result #{index} is not a SourceItem",
                        )
                    )
                    continue
                link = entry.link
                if not link:
                    failures.append(
                        CandidateFailure(
                            url=None,
                            kind=CANDIDATE_NO_LINK,
                            detail=(
                                "discovery lead carried no link; only links may "
                                "be re-collected"
                            ),
                        )
                    )
                    continue
                normalized = normalize_url(link)
                if _host_of(link) not in query.source_domains:
                    failures.append(
                        CandidateFailure(
                            url=_safe_url(normalized),
                            kind=CANDIDATE_DOMAIN_OUT_OF_SCOPE,
                            detail=(
                                f"candidate host {_host_of(link)!r} is not among "
                                "the query source_domains"
                            ),
                        )
                    )
                    continue
                # A scholarly API endpoint (e.g. a Europe PMC REST search URL)
                # is metadata plumbing, not an article: resolve it to the
                # canonical article URL or drop it for audit.  Without a
                # configured resolver the drop is a pure local
                # classification and costs no candidate budget.
                identifier = scholarly_api_identifier(link)
                if identifier is not None and self._scholarly is None:
                    failures.append(
                        CandidateFailure(
                            url=_safe_url(normalized),
                            kind=CANDIDATE_UNRESOLVED_LEAD,
                            detail=(
                                f"discovery lead is the scholarly API endpoint "
                                f"for {identifier}; no scholarly adapter is "
                                "configured to resolve it to the canonical "
                                "article URL, and an API endpoint is never "
                                "fetched as an article"
                            ),
                        )
                    )
                    continue
                if normalized in seen:
                    duplicates.append(_safe_url(normalized))
                    continue
                seen.add(normalized)
                collect_url = normalized
                if identifier is not None:
                    resolution = await self._resolve_scholarly(
                        normalized, identifier
                    )
                    if isinstance(resolution, CandidateFailure):
                        failures.append(resolution)
                        # A failed resolution consumed the candidate slot,
                        # exactly like a failed intake fetch would.
                        attempted += 1
                        if query.concept_id in concept_budgets:
                            concept_attempted[query.concept_id] = (
                                concept_attempted.get(query.concept_id, 0) + 1
                            )
                        continue
                    collect_url = resolution
                    if collect_url in seen:
                        duplicates.append(_safe_url(collect_url))
                        continue
                    seen.add(collect_url)
                attempted += 1
                if query.concept_id in concept_budgets:
                    concept_attempted[query.concept_id] = (
                        concept_attempted.get(query.concept_id, 0) + 1
                    )
                success = await self._collect(collect_url, process=process)
                if isinstance(success, CandidateSuccess):
                    successes.append(success)
                else:
                    failures.append(success)

        return QueryExecution(
            query=query.query,
            concept_id=query.concept_id,
            window=query.window,
            reason=query.reason,
            source_domains=query.source_domains,
            discovered=len(leads) if leads is not None else 0,
            successes=tuple(successes),
            failures=tuple(failures),
            duplicates=tuple(duplicates),
            provider_error=provider_error,
        )

    async def _resolve_scholarly(
        self, lead_url: str, identifier: str
    ) -> str | CandidateFailure:
        """Resolve one scholarly API endpoint lead to its canonical article URL.

        The identifier literal (``PMID:...`` / ``PMCID:...``) is resolved
        through the injected scholarly adapter — the authoritative Europe
        PMC / PMC path — and the canonical article link (DOI landing page,
        PMC article page, or PubMed record) is returned normalized.  Any
        resolution failure becomes an auditable ``unresolved_lead`` failure
        record; the API endpoint itself is never returned as a fetch target.
        """
        try:
            resolved = await self._scholarly.fetch(identifier)
        except SourceFetchError as error:
            return CandidateFailure(
                url=_safe_url(lead_url),
                kind=CANDIDATE_UNRESOLVED_LEAD,
                detail=(
                    f"scholarly API lead for {identifier} could not be "
                    f"resolved to the canonical article "
                    f"({error.kind.value}): {_safe_error_detail(error)}"
                ),
            )
        except Exception as error:
            return CandidateFailure(
                url=_safe_url(lead_url),
                kind=CANDIDATE_UNRESOLVED_LEAD,
                detail=(
                    f"scholarly API lead for {identifier} could not be "
                    f"resolved: {type(error).__name__}: "
                    f"{_safe_error_detail(error)}"
                ),
            )
        link = getattr(resolved, "link", None)
        if not isinstance(link, str) or not link.strip():
            return CandidateFailure(
                url=_safe_url(lead_url),
                kind=CANDIDATE_UNRESOLVED_LEAD,
                detail=(
                    f"scholarly API lead for {identifier} resolved to a "
                    "record without a canonical link"
                ),
            )
        return normalize_url(link)

    async def _collect(
        self, url: str, *, process: bool
    ) -> CandidateSuccess | CandidateFailure:
        """Re-fetch one candidate URL through the authoritative intake."""
        try:
            report = await self._intake.fetch_source(
                url, kind=self._kind, process=process
            )
        except SourceFetchError as error:
            return CandidateFailure(
                url=_safe_url(error.url),
                kind=error.kind.value,
                detail=_safe_error_detail(error),
            )
        except Exception as error:
            return CandidateFailure(
                url=_safe_url(url),
                kind=type(error).__name__,
                detail=f"{type(error).__name__}: {_safe_error_detail(error)}",
            )
        if not isinstance(report, SourceFetchReport):
            return CandidateFailure(
                url=_safe_url(url),
                kind=CANDIDATE_INVALID_INTAKE,
                detail="intake must return a SourceFetchReport",
            )
        if not report.items:
            return CandidateFailure(
                url=_safe_url(url),
                kind=CANDIDATE_NO_CONTENT,
                detail=(
                    "intake returned no collectible items; a URL without a "
                    "real body is not evidence"
                ),
            )
        return CandidateSuccess(
            url=_safe_url(url),
            material_ids=tuple(item.material_id for item in report.items),
            report=_safe_fetch_report(report),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise RuntimeError("clock must return timezone-aware datetimes")
        return value


__all__ = [
    "CANDIDATE_DOMAIN_OUT_OF_SCOPE",
    "CANDIDATE_INVALID_INTAKE",
    "CANDIDATE_INVALID_LEAD",
    "CANDIDATE_NO_CONTENT",
    "CANDIDATE_NO_LINK",
    "CANDIDATE_UNRESOLVED_LEAD",
    "DEFAULT_INTAKE_KIND",
    "DEFAULT_MAX_CANDIDATES_PER_QUERY",
    "DEFAULT_SEARCH_RETRIES",
    "DEFAULT_SEARCH_TIMEOUT",
    "CandidateFailure",
    "CandidateSuccess",
    "QueryExecution",
    "ResearchExecutionReport",
    "ResearchExecutor",
    "ScholarlyLeadResolver",
    "SourceIntake",
]
