"""Evidence-discovery research planning (FR-1.14 ~ FR-1.17, FR-3.8, FR-4.7).

``ResearchPlanner`` turns a material (optionally enriched with an extraction)
into an auditable, whitelist-gated ``ResearchPlan`` of time-sliced windows
and search queries, using the ``source_selector`` LLM role when one is
injected and a deterministic whitelist-derived fallback otherwise.  This
package performs no network I/O of its own: the :class:`SearchProvider`
protocol is executed by :class:`FirecrawlSearchProvider`, a Firecrawl v2
adapter that talks only through an injected async JSON client, and plans are
run end to end by :class:`ResearchExecutor`, whose candidate URLs are
re-collected as evidence through an injected :class:`SourceIntake`
(``PrismAPI.fetch_source``) rather than any search payload.
"""

from .models import (
    PLAN_ORIGINS,
    PLAN_ORIGIN_FALLBACK,
    PLAN_ORIGIN_LLM,
    PRIORITY_MAX,
    PRIORITY_MIN,
    RESEARCH_PHASES,
    SOURCE_TYPES,
    ResearchPlan,
    ResearchWindow,
    SearchQuery,
    SourceCandidate,
)
from .executor import (
    CANDIDATE_DOMAIN_OUT_OF_SCOPE,
    CANDIDATE_INVALID_INTAKE,
    CANDIDATE_INVALID_LEAD,
    CANDIDATE_NO_CONTENT,
    CANDIDATE_NO_LINK,
    CandidateFailure,
    CandidateSuccess,
    QueryExecution,
    ResearchExecutionReport,
    ResearchExecutor,
    SourceIntake,
)
from .firecrawl import (
    DEFAULT_BASE_URL,
    FIRECRAWL_API_KEY_ENV,
    MAP_CANDIDATE_TYPE,
    FirecrawlBlockedError,
    FirecrawlError,
    FirecrawlHttpError,
    FirecrawlJsonError,
    FirecrawlSchemaError,
    FirecrawlSearchProvider,
    FirecrawlTimeoutError,
    FirecrawlTransportError,
    JsonClient,
    JsonHttpResponse,
)
from .firecrawl_http import (
    DEFAULT_MAX_RESPONSE_BYTES,
    FirecrawlHttpClientError,
    FirecrawlHttpRedirectError,
    FirecrawlHttpResponseTooLargeError,
    FirecrawlHttpTimeoutError,
    FirecrawlHttpTransportError,
    FirecrawlHttpUnicodeError,
    FirecrawlHttpUrlError,
    FirecrawlJsonHttpClient,
    FirecrawlNoRedirectHandler,
)
from .planner import SOURCE_SELECTOR_ROLE, ResearchPlanError, ResearchPlanner
from .provider import SearchProvider

__all__ = [
    "CANDIDATE_DOMAIN_OUT_OF_SCOPE",
    "CANDIDATE_INVALID_INTAKE",
    "CANDIDATE_INVALID_LEAD",
    "CANDIDATE_NO_CONTENT",
    "CANDIDATE_NO_LINK",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "FIRECRAWL_API_KEY_ENV",
    "MAP_CANDIDATE_TYPE",
    "PLAN_ORIGINS",
    "PLAN_ORIGIN_FALLBACK",
    "PLAN_ORIGIN_LLM",
    "PRIORITY_MAX",
    "PRIORITY_MIN",
    "RESEARCH_PHASES",
    "SOURCE_SELECTOR_ROLE",
    "SOURCE_TYPES",
    "CandidateFailure",
    "CandidateSuccess",
    "FirecrawlBlockedError",
    "FirecrawlError",
    "FirecrawlHttpClientError",
    "FirecrawlHttpError",
    "FirecrawlHttpRedirectError",
    "FirecrawlHttpResponseTooLargeError",
    "FirecrawlHttpTimeoutError",
    "FirecrawlHttpTransportError",
    "FirecrawlHttpUnicodeError",
    "FirecrawlHttpUrlError",
    "FirecrawlJsonError",
    "FirecrawlJsonHttpClient",
    "FirecrawlNoRedirectHandler",
    "FirecrawlSchemaError",
    "FirecrawlSearchProvider",
    "FirecrawlTimeoutError",
    "FirecrawlTransportError",
    "JsonClient",
    "JsonHttpResponse",
    "QueryExecution",
    "ResearchExecutionReport",
    "ResearchExecutor",
    "ResearchPlan",
    "ResearchPlanError",
    "ResearchPlanner",
    "ResearchWindow",
    "SearchProvider",
    "SearchQuery",
    "SourceCandidate",
    "SourceIntake",
]
