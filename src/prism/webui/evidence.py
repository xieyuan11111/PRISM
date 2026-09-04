"""Dependency-free controller/view-model seam for the PRISM evidence browser.

The controller is the evidence library's only data boundary (FR-8.6): every
read goes through the injected async facade — the real
:class:`prism.api.PrismAPI` with its ``search`` method (or an equivalent
duck-typed object exposing the legacy ``search_evidence`` compatibility name)
— never the SQLite store or the corpus files directly.  What the controller
adds is exactly the view layer: typed validation of every filter BEFORE any
facade call, an honest pagination window, and JSON-safe hit views that keep
each result's source id, corpus path, snippet, URL and publication time
verbatim for the NiceGUI page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class EvidenceFacade(Protocol):
    """The facade operation used by the evidence browser (FR-8.6)."""

    async def search(
        self,
        query: str | None = None,
        *,
        case_tag: str | None = None,
        source: str | None = None,
        type: str | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> object: ...


def parse_time_bound(name: str, value: datetime | str) -> datetime:
    """Parse one user-supplied time filter into a timezone-aware datetime.

    Accepts an aware :class:`~datetime.datetime` directly or an ISO 8601
    string; a naive value, an unparseable string or another type raises
    ``ValueError``/``TypeError`` explicitly so the caller can surface the
    error instead of silently querying with a wrong instant.  ``name`` names
    the filter in every error message.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(
                f"{name} must be an ISO 8601 datetime with a timezone"
            )
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"{name} must be an ISO 8601 datetime with a timezone"
            ) from None
    else:
        raise TypeError(f"{name} must be a datetime or an ISO 8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{name} must be timezone-aware (include a UTC offset, "
            "e.g. 2026-01-01T00:00:00+00:00)"
        )
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_text(name: str, value: str) -> str | None:
    """Validate one free-text filter; blank means neutral (``None``)."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value.strip() or None


def _optional_int(name: str, value: int, *, minimum: int,
                  maximum: int | None = None) -> int:
    """Validate one pagination integer; bools are rejected explicitly."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"at most {maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"{name} must be {bound}")
    return value


def hit_view(hit: object) -> dict[str, Any]:
    """Project one search hit into JSON-safe view data.

    The corpus path (``path`` on the store's ``SearchHit``), snippet, URL,
    publication time, case tags and every scholarly/retrieval field survive
    the projection verbatim; portable locator fields (paragraph/page/quote)
    are kept whenever the facade result carries them and stay ``None``
    otherwise — nothing is invented here.
    """
    return {
        "source_id": getattr(hit, "source_id"),
        "title": getattr(hit, "title"),
        "source": getattr(hit, "source"),
        "type": getattr(hit, "type"),
        "published_at": _iso(getattr(hit, "published_at")),
        "case_tags": list(getattr(hit, "case_tags", ()) or ()),
        "corpus_path": getattr(hit, "path"),
        "raw_path": getattr(hit, "raw_path", None),
        "url": getattr(hit, "url", None),
        "snippet": getattr(hit, "snippet", None),
        "retrieval_level": getattr(hit, "retrieval_level", None),
        "access_level": getattr(hit, "access_level", None),
        "doi": getattr(hit, "doi", None),
        "authors": list(getattr(hit, "authors", ()) or ()),
        "container_title": getattr(hit, "container_title", None),
        "pmid": getattr(hit, "pmid", None),
        "pmcid": getattr(hit, "pmcid", None),
        "paragraph": getattr(hit, "paragraph", None),
        "page": getattr(hit, "page", None),
        "quote": getattr(hit, "quote", None),
    }


class EvidenceBrowserController:
    """View-model adapter over the shared PrismAPI search facade.

    All operations are async because the facade is async; all results are
    JSON-safe view data for the page.  Invalid filters — naive or unparseable
    time bounds, an inverted time window, wrong types, non-positive or
    oversized pagination — raise explicit exceptions BEFORE any facade call,
    and a validation failure never falls back to a different query.
    """

    page_size = DEFAULT_PAGE_SIZE

    def __init__(self, api: EvidenceFacade) -> None:
        method = getattr(api, "search", None)
        if not callable(method):
            method = getattr(api, "search_evidence", None)
            if not callable(method):
                raise TypeError(
                    "api must provide search() or search_evidence()"
                )
        self._api = api
        self._search = method

    async def browse(
        self,
        query: str = "",
        *,
        case: str = "",
        source: str = "",
        type: str = "",
        published_after: datetime | str | None = None,
        published_before: datetime | str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Load one page of evidence through the facade's typed filters.

        ``query``/``case``/``source``/``type`` and the time bounds are
        forwarded exactly as ``PrismAPI.search`` expects them (blank text
        means neutral); the page window is requested as ``limit``/``offset``.
        To report ``has_more`` honestly without inventing a total, one extra
        row is probed and trimmed from the returned view.
        """
        text_query = _optional_text("query", query)
        case_tag = _optional_text("case", case)
        text_source = _optional_text("source", source)
        text_type = _optional_text("type", type)
        bound_after = (
            parse_time_bound("published_after", published_after)
            if published_after is not None else None
        )
        bound_before = (
            parse_time_bound("published_before", published_before)
            if published_before is not None else None
        )
        if (
            bound_after is not None
            and bound_before is not None
            and bound_after > bound_before
        ):
            raise ValueError(
                "published_after must not be later than published_before"
            )
        number = _optional_int("page", page, minimum=1)
        size = _optional_int("page_size", page_size, minimum=1,
                             maximum=MAX_PAGE_SIZE)
        offset = (number - 1) * size
        hits = tuple(await self._search(
            text_query,
            case_tag=case_tag,
            source=text_source,
            type=text_type,
            published_after=bound_after,
            published_before=bound_before,
            limit=size + 1,
            offset=offset,
        ))
        has_more = len(hits) > size
        return {
            "query": text_query,
            "filters": {
                "case": case_tag,
                "source": text_source,
                "type": text_type,
                "published_after": _iso(bound_after),
                "published_before": _iso(bound_before),
            },
            "page": number,
            "page_size": size,
            "offset": offset,
            "count": min(len(hits), size),
            "has_more": has_more,
            "has_previous": number > 1,
            "results": [hit_view(hit) for hit in hits[:size]],
        }


_HIT_COLUMNS = [
    {"name": "source_id", "label": "Source id", "field": "source_id",
     "align": "left", "sortable": True},
    {"name": "title", "label": "Title", "field": "title", "align": "left"},
    {"name": "type", "label": "Type", "field": "type", "align": "left",
     "sortable": True},
    {"name": "source", "label": "Origin", "field": "source", "align": "left",
     "sortable": True},
    {"name": "published_at", "label": "Published", "field": "published_at",
     "align": "left", "sortable": True},
    {"name": "case_tags", "label": "Cases", "field": "case_tags",
     "align": "left"},
    {"name": "corpus_path", "label": "Corpus path", "field": "corpus_path",
     "align": "left"},
    {"name": "url", "label": "URL", "field": "url", "align": "left"},
]


def build_evidence_page(
    controller: EvidenceBrowserController, ui: Any, *, title: str = "PRISM Evidence"
) -> Any:
    """Register the ``/evidence`` browser page on the given ``ui`` module.

    The ``ui`` module is injected so the page construction — the filter
    controls, results table and their handlers — is a seam testable without
    NiceGUI installed; every handler delegates to the controller and reports
    explicit errors in the message label instead of swallowing them.  The
    page only reads what the facade returns: no upload, no corpus access.
    """
    @ui.page("/evidence")
    def evidence_page() -> None:
        message = ui.label("Search the evidence library to begin.")
        current_page = [1]

        with ui.card().classes("w-full"):
            ui.label("Evidence filters").classes("text-bold")
            with ui.row():
                query_input = ui.input(label="Query", placeholder="full text")
                case_input = ui.input(label="Case", placeholder="case-rates")
                source_input = ui.input(label="Source", placeholder="example.gov")
                type_input = ui.input(label="Type", placeholder="policy")
            with ui.row():
                after_input = ui.input(
                    label="Published after (ISO 8601, timezone-aware)",
                    placeholder="2026-01-01T00:00:00+00:00",
                )
                before_input = ui.input(
                    label="Published before (ISO 8601, timezone-aware)",
                    placeholder="2026-03-01T00:00:00+00:00",
                )

        def _report(text: str) -> None:
            message.text = text
            message.update()

        async def _load_page(number: int) -> None:
            try:
                view = await controller.browse(
                    query_input.value or "",
                    case=case_input.value or "",
                    source=source_input.value or "",
                    type=type_input.value or "",
                    published_after=after_input.value or None,
                    published_before=before_input.value or None,
                    page=number,
                )
            except Exception as error:
                _report(f"error searching evidence: {error}")
                return
            current_page[0] = view["page"]
            results_table.rows = view["results"]
            results_table.update()
            _report(
                f"{view['count']} result(s) on page {view['page']}"
                + (" (more available)" if view["has_more"] else "")
            )

        async def _search(event: Any = None) -> None:
            await _load_page(1)

        async def _next(event: Any = None) -> None:
            await _load_page(current_page[0] + 1)

        async def _previous(event: Any = None) -> None:
            if current_page[0] <= 1:
                _report("already on the first page")
                return
            await _load_page(current_page[0] - 1)

        with ui.card().classes("w-full"):
            results_table = ui.table(columns=_HIT_COLUMNS, rows=[])
            with ui.row():
                ui.button("Search", on_click=_search)
                ui.button("Previous", on_click=_previous)
                ui.button("Next", on_click=_next)

    return evidence_page


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "EvidenceBrowserController",
    "EvidenceFacade",
    "MAX_PAGE_SIZE",
    "build_evidence_page",
    "hit_view",
    "parse_time_bound",
]
