"""Scholarly API discovery-lead detection for the research executor.

Search engines index scholarly REST endpoints — Europe PMC's
``webservices/rest/search`` and the per-record ``fullTextXML`` URLs — so a
discovery lead can be metadata plumbing instead of an article.  Fetching such
a URL as an article wastes the candidate (real run 2026-09-16: three Europe
PMC REST search leads failed intake as ``timeout``/``http_status`` while the
same run ingested the canonical DOI articles successfully).

:func:`scholarly_api_identifier` recognizes exactly these endpoints — approved
EBI host plus anchored REST path, never a loose substring match — and returns
an explicit ``PMID:``/``PMCID:`` literal that the existing scholarly adapter
(:class:`prism.sources.ScholarlyMetadataClient`) resolves to the canonical
article URL (DOI landing page, PMC article page, or PubMed record).  An
identifier embedded in any other URL is never read.  This module performs no
I/O; resolution itself is injected into
:class:`prism.research.executor.ResearchExecutor`.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_EUROPEPMC_API_HOSTS = frozenset({"www.ebi.ac.uk", "ebi.ac.uk"})
_EUROPEPMC_SEARCH_PATH = "/europepmc/webservices/rest/search"
# The per-record open-access full-text endpoint: .../rest/<PMCID>/fullTextXML
_EUROPEPMC_FULLTEXT_PATH = re.compile(
    r"^/europepmc/webservices/rest/(PMC\d{1,9})/fullTextXML$", re.IGNORECASE
)
_PMCID_QUERY_TOKEN = re.compile(r"PMCID:(PMC\d{1,9})", re.IGNORECASE)
_PMID_QUERY_TOKEN = re.compile(r"EXT_ID:(\d{1,9})")


def scholarly_api_identifier(url: str) -> str | None:
    """Return a ``PMID:``/``PMCID:`` literal when ``url`` is a scholarly API
    endpoint, else ``None``.

    Only the Europe PMC REST search endpoint (identifier read from its
    decoded ``query`` parameter) and the per-record ``fullTextXML`` endpoint
    (identifier read from its path) are recognized, and only on an approved
    EBI host.  Everything else — including foreign URLs that merely mention
    ``EXT_ID:`` — returns ``None``.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if host not in _EUROPEPMC_API_HOSTS:
        return None
    path = parts.path or "/"
    fulltext = _EUROPEPMC_FULLTEXT_PATH.fullmatch(path)
    if fulltext is not None:
        return f"PMCID:{fulltext.group(1).upper()}"
    if path.rstrip("/") != _EUROPEPMC_SEARCH_PATH:
        return None
    query_values = [
        value
        for key, values in parse_qs(parts.query or "").items()
        if key == "query"
        for value in values
    ]
    for value in query_values:
        for token in re.split(r"[\s&]+", value):
            pmcid = _PMCID_QUERY_TOKEN.fullmatch(token)
            if pmcid is not None:
                return f"PMCID:{pmcid.group(1).upper()}"
            pmid = _PMID_QUERY_TOKEN.fullmatch(token)
            if pmid is not None:
                return f"PMID:{pmid.group(1)}"
    return None


__all__ = ["scholarly_api_identifier"]
