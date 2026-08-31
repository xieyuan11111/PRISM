"""Public PRISM command-line shell."""

from .main import (
    PrismAPIProtocol,
    build_parser,
    handle_ingest,
    handle_search,
    handle_timeline,
    main,
)

__all__ = [
    "PrismAPIProtocol",
    "build_parser",
    "handle_ingest",
    "handle_search",
    "handle_timeline",
    "main",
]
