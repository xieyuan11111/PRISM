"""Public sources collection API (RSS/Atom feeds and public web pages).

PRISM module for FR-1.1 material collection: fetch whitelisted public
sources through an injectable async HTTP getter, parse them with the
standard library only, classify every failure, deduplicate stably, and hand
``SourceItem`` objects to :class:`~prism.ingestion.IngestionService`.  This
layer never writes the corpus and never calls the network on its own.
"""

from .feeds import FeedFetcher, parse_feed
from .http import (
    HttpGetter,
    HttpGetterError,
    HttpGetterNoRedirectHandler,
    HttpGetterResponseTooLargeError,
    HttpResponse,
    UrllibHttpGetter,
)
from .models import (
    FailureKind,
    FetchFailure,
    FetchResult,
    SourceFetchError,
    SourceFetcher,
    SourceItem,
)
from .pages import PageFetcher, extract_page
from .scholarly import (
    AcademicRecord,
    CrossrefClient,
    EuropePmcClient,
    MetadataClient,
    OpenAlexClient,
    ScholarlyMetadataClient,
    ScholarlyRecord,
    extract_doi,
    extract_pmcid,
    extract_pmid,
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
    redact_audit_text,
)
from .service import (
    KIND_AUTO,
    FetchBatch,
    SourceService,
    validate_public_url,
)
from .reprocess import (
    CLASSIFICATIONS,
    ReprocessCandidate,
    ReprocessRun,
    run_identifier_reprocess,
)
from .urls import host_rejection_reason, normalize_url

__all__ = [
    "FailureKind",
    "FetchBatch",
    "FetchFailure",
    "FetchResult",
    "FeedFetcher",
    "HttpGetter",
    "HttpGetterError",
    "HttpGetterNoRedirectHandler",
    "HttpGetterResponseTooLargeError",
    "HttpResponse",
    "AcademicRecord",
    "CrossrefClient",
    "EuropePmcClient",
    "MetadataClient",
    "OpenAlexClient",
    "ScholarlyMetadataClient",
    "ScholarlyRecord",
    "extract_doi",
    "extract_pmcid",
    "extract_pmid",
    "normalize_doi",
    "normalize_pmcid",
    "normalize_pmid",
    "redact_audit_text",
    "KIND_AUTO",
    "CLASSIFICATIONS",
    "PageFetcher",
    "SourceFetchError",
    "SourceFetcher",
    "SourceItem",
    "ReprocessCandidate",
    "ReprocessRun",
    "SourceService",
    "extract_page",
    "host_rejection_reason",
    "normalize_url",
    "parse_feed",
    "validate_public_url",
    "run_identifier_reprocess",
    "UrllibHttpGetter",
]
