"""Future search-adapter boundary for PRISM evidence discovery.

The research planner only *plans* retrieval: it produces
:class:`~prism.research.models.SearchQuery` objects bound to whitelisted
domains.  Executing those queries against an external search backend (for
example a Firecrawl-backed adapter) is a deliberately separate concern.
A future module implements this protocol, maps results to
:class:`~prism.sources.SourceItem` objects whose domains still pass
whitelist validation, and hands them to
:class:`~prism.ingestion.IngestionService`.  No implementation lives in
this package, and nothing here touches the network or Firecrawl.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from prism.sources import SourceItem

from .models import SearchQuery


@runtime_checkable
class SearchProvider(Protocol):
    """Executable-search seam declared for future adapters (not implemented)."""

    name: str

    async def search(
        self, query: SearchQuery, *, timeout: float = 10.0
    ) -> tuple[SourceItem, ...]:
        """Return source items for one planned query; never invent domains."""
        ...
