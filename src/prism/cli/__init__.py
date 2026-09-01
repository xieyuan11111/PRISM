"""Public PRISM command-line shell."""

from .main import (
    PrismAPIProtocol,
    build_parser,
    handle_discover,
    handle_fetch,
    handle_fetch_all,
    handle_ingest,
    handle_research,
    handle_report,
    handle_search,
    handle_timeline,
    main,
)

__all__ = [
    "PrismAPIProtocol",
    "build_parser",
    "handle_discover",
    "handle_fetch",
    "handle_fetch_all",
    "handle_ingest",
    "handle_research",
    "handle_report",
    "handle_search",
    "handle_timeline",
    "main",
]
