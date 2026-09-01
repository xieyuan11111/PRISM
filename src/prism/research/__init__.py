"""Evidence-discovery research planning (FR-1.14 ~ FR-1.17, FR-3.8, FR-4.7).

``ResearchPlanner`` turns a material (optionally enriched with an extraction)
into an auditable, whitelist-gated ``ResearchPlan`` of time-sliced windows
and search queries, using the ``source_selector`` LLM role when one is
injected and a deterministic whitelist-derived fallback otherwise.  This
package performs no network I/O of its own: the :class:`SearchProvider`
protocol is executed by :class:`FirecrawlSearchProvider`, a Firecrawl v2
adapter that talks only through an injected async JSON client.
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
from .planner import SOURCE_SELECTOR_ROLE, ResearchPlanError, ResearchPlanner
from .provider import SearchProvider

__all__ = [
    "DEFAULT_BASE_URL",
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
    "FirecrawlBlockedError",
    "FirecrawlError",
    "FirecrawlHttpError",
    "FirecrawlJsonError",
    "FirecrawlSchemaError",
    "FirecrawlSearchProvider",
    "FirecrawlTimeoutError",
    "FirecrawlTransportError",
    "JsonClient",
    "JsonHttpResponse",
    "ResearchPlan",
    "ResearchPlanError",
    "ResearchPlanner",
    "ResearchWindow",
    "SearchProvider",
    "SearchQuery",
    "SourceCandidate",
]
