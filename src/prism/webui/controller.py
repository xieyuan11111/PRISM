"""Dependency-free controller/view-model seam for the PRISM case-home WebUI.

The controller is the WebUI's only data boundary: every read goes through the
injected async facade (the real :class:`prism.api.PrismAPI` or an equivalent
duck-typed object), never the graph, the store or an LLM client, and nothing
here re-implements temporal logic — filtering, snapshot projection and the
knowledge boundary stay behind ``PrismAPI.case_overviews`` /
``PrismAPI.case_overview`` / ``PrismAPI.query_historical_snapshot``.  What the
controller adds is exactly the view layer: JSON-safe view data for the NiceGUI
page, explicit validation errors (never silently swallowed or re-targeted at a
different API method) and a remembered case selection.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Protocol

from prism.analyzer import (
    ENTRY_KINDS,
    HistoricalCaseState,
    require_stage,
)

from .journey import material_row
from .reports import report_detail_url, report_row


class PrismFacade(Protocol):
    """The facade operations used by the case home (FR-8.3/FR-8.4/FR-8.10)."""

    async def case_overviews(self, **filters: object) -> object: ...

    async def case_overview(self, case_id: str) -> object: ...

    async def query_historical_snapshot(
        self,
        case_id: str,
        as_of: datetime,
        *,
        stage: str | None = None,
        kinds: Iterable[str] | None = None,
    ) -> HistoricalCaseState: ...


def parse_as_of(value: datetime | str) -> datetime:
    """Parse one user-supplied ``as_of`` into a timezone-aware datetime.

    Accepts an aware :class:`~datetime.datetime` directly or an ISO 8601
    string; a naive value, an unparseable string or another type raises
    ``ValueError``/``TypeError`` explicitly so the caller can surface the
    error instead of silently querying with a wrong instant.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(
                "as_of must be an ISO 8601 datetime with a timezone"
            )
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                "as_of must be an ISO 8601 datetime with a timezone"
            ) from None
    else:
        raise TypeError("as_of must be a datetime or an ISO 8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "as_of must be timezone-aware (include a UTC offset, "
            "e.g. 2026-02-02T00:00:00+00:00)"
        )
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json_safe(value: Any) -> Any:
    """Convert a frozen domain object tree into JSON-safe view data.

    Datetimes become ISO 8601 strings, dataclasses become dicts (with tuples
    and sets becoming lists) so every locator, source id and layer survives
    the projection verbatim.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _json_safe(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    return value


def _case_view(overview: object) -> dict[str, Any]:
    """Project one case overview into a flat, JSON-safe table row."""
    gaps = bool(getattr(overview, "has_unresolved_gaps", False))
    conflicts = bool(getattr(overview, "has_unresolved_conflicts", False))
    return {
        "case_id": getattr(overview, "case_id"),
        "case_type": getattr(overview, "case_type"),
        "name": getattr(overview, "name"),
        "status": getattr(overview, "status"),
        "material_count": getattr(overview, "material_count"),
        "earliest_observed_at": _iso(getattr(overview, "earliest_observed_at")),
        "latest_observed_at": _iso(getattr(overview, "latest_observed_at")),
        "latest_node_at": _iso(getattr(overview, "latest_node_at")),
        "last_updated_at": _iso(getattr(overview, "last_updated_at")),
        "has_unresolved_gaps": gaps,
        "has_unresolved_conflicts": conflicts,
        "unresolved": gaps or conflicts,
    }


def snapshot_view(state: HistoricalCaseState) -> dict[str, Any]:
    """Project one historical snapshot into JSON-safe view data.

    Every bucket is retained verbatim — effective and invalidated facts,
    interpretations (claims), relations, evidence gaps, and every source and
    evidence locator attached to each entry.
    """
    return _json_safe(state)


_TIMELINE_BUCKETS = (
    ("nodes", False),
    ("facts", False),
    ("interpretations", False),
    ("relations", False),
    ("invalidated_facts", True),
)


def _timeline_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a JSON-safe snapshot without changing its temporal meaning."""
    rows: list[dict[str, Any]] = []
    for bucket, invalidated in _TIMELINE_BUCKETS:
        for entry in snapshot.get(bucket) or ():
            row = dict(entry)
            row["invalidated"] = invalidated
            rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("valid_at") or ""),
            str(row.get("episode_key") or ""),
            str(row.get("kind") or ""),
            bool(row.get("invalidated")),
        ),
    )


#: The lifecycle filters the journey query accepts (Phase C linkage); the
#: same vocabulary the facade and the journey list page use.
_LIFECYCLE_STATUSES = frozenset(
    {"pending", "failed", "committed", "unknown"}
)

#: How many rows the case home's "recent" panels request by default; a
#: presentation cap only — the facade still decides membership and order.
CASE_LINK_LIMIT = 5

_MIN_INSTANT = datetime.min.replace(tzinfo=timezone.utc)


def _validated_limit(limit: Any) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer or None")
    return limit


class CaseHomeController:
    """View-model adapter over the shared PrismAPI facade.

    All operations are async because the facade is async; all results are
    JSON-safe view data for the page.  Invalid inputs (empty case id, naive
    or unparseable ``as_of``, invented stage/kind, unknown case) raise
    explicit exceptions BEFORE any snapshot read, and validation failures
    never fall back to calling a different facade method.
    """

    def __init__(self, api: PrismFacade) -> None:
        for name in (
            "case_overviews",
            "case_overview",
            "query_historical_snapshot",
        ):
            if not callable(getattr(api, name, None)):
                raise TypeError(f"api must provide {name}()")
        self._api = api
        self._selected_case_id: str | None = None
        self._loaded_timeline: list[dict[str, Any]] = []
        # Phase C linkage capabilities, detected the same way the page
        # factory gates richer pages: an older case-only facade keeps
        # building this controller and the linkage stays explicitly
        # unavailable instead of crashing or faking empty panels.
        journeys = getattr(api, "material_journeys", None)
        self._material_journeys = journeys if callable(journeys) else None
        report_versions = getattr(api, "report_versions", None)
        self._report_versions = (
            report_versions if callable(report_versions) else None
        )

    @property
    def selected_case_id(self) -> str | None:
        """The case id remembered by the last successful :meth:`select_case`."""
        return self._selected_case_id

    @property
    def materials_available(self) -> bool:
        """Whether the facade provides the read-only material-run listing."""
        return self._material_journeys is not None

    @property
    def reports_available(self) -> bool:
        """Whether the facade provides the read-only report-version listing."""
        return self._report_versions is not None

    async def load_cases(
        self,
        *,
        search: str = "",
        case_type: str = "",
        status: str = "",
        unresolved_only: bool = False,
    ) -> dict[str, Any]:
        """Load case overviews through the facade with the typed filters.

        ``case_type``/``status``/``unresolved_only`` are forwarded to
        ``PrismAPI.case_overviews`` (exact-match ledger filters); the free-text
        ``search`` is a client-side substring match over the loaded case ids
        and names, because the facade deliberately has no fuzzy case lookup.
        """
        if not isinstance(search, str) or not isinstance(case_type, str):
            raise TypeError("search and case_type must be strings")
        if not isinstance(status, str):
            raise TypeError("status must be a string")
        overviews = await self._api.case_overviews(
            case_type=case_type.strip() or None,
            status=status.strip() or None,
            unresolved_only=bool(unresolved_only),
        )
        views = [_case_view(item) for item in tuple(overviews)]
        needle = search.strip().lower()
        if needle:
            views = [
                view
                for view in views
                if needle in str(view["case_id"]).lower()
                or needle in str(view["name"]).lower()
            ]
        return {"cases": views, "count": len(views)}

    async def select_case(self, case_id: str) -> dict[str, Any]:
        """Validate and remember one case; unknown ids raise ``LookupError``."""
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        overview = await self._api.case_overview(case_id.strip())
        self._selected_case_id = case_id.strip()
        return _case_view(overview)

    async def load_snapshot(
        self,
        case_id: str,
        as_of: datetime | str,
        *,
        stage: str | None = None,
        kinds: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Load the formal historical snapshot for one case at ``as_of``.

        Validation is explicit and happens before any snapshot read: the
        case id must be non-empty, ``as_of`` timezone-aware (datetime or ISO
        8601 string), ``stage`` one of the deterministic recorded stages and
        every kind a known entry kind.  The case itself is validated through
        ``PrismAPI.case_overview`` so an unknown case surfaces as
        ``LookupError`` — never as an empty snapshot pretending to be one.
        """
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        case_id = case_id.strip()
        instant = parse_as_of(as_of)
        selected_stage = require_stage(stage or None)
        normalized_kinds = self._normalize_kinds(kinds)
        overview = await self._api.case_overview(case_id)
        state = await self._api.query_historical_snapshot(
            case_id, instant, stage=selected_stage, kinds=normalized_kinds
        )
        snapshot = snapshot_view(state)
        timeline = _timeline_rows(snapshot)
        self._loaded_timeline = timeline
        return {
            "case": _case_view(overview),
            "snapshot": snapshot,
            "timeline": _json_safe(timeline),
        }

    def timeline_rows(self) -> list[dict[str, Any]]:
        """Return timeline points from the last successful snapshot load.

        This is a presentation projection only: it never calls the facade and
        performs no temporal filtering. Effective and invalidated entries are
        distinguished using the analyzer snapshot buckets.
        """
        return _json_safe(self._loaded_timeline)

    def select_timeline_point(self, episode_key: str) -> dict[str, Any]:
        """Select one point from the already-loaded snapshot by stable key."""
        if not isinstance(episode_key, str) or not episode_key.strip():
            raise ValueError("episode_key must be a non-empty string")
        key = episode_key.strip()
        for row in self._loaded_timeline:
            if row.get("episode_key") == key:
                return _json_safe(row)
        raise LookupError(f"timeline point {key!r} is not in the loaded snapshot")

    async def load_case_materials(
        self,
        case_id: str,
        *,
        status: str | None = None,
        limit: int | None = CASE_LINK_LIMIT,
    ) -> dict[str, Any]:
        """Load the selected case's recent material runs (Phase C).

        A pure projection of ``PrismAPI.material_journeys`` — the durable
        outcome ledger hydrated across sessions, including failures — so no
        new fact view is assembled here (H-6).  Inputs are validated before
        any read; a facade without the operation raises ``RuntimeError``
        instead of answering a fabricated empty list; an unknown case
        surfaces as ``LookupError`` through ``case_overview``; and a facade
        failure propagates unchanged — an unreadable ledger is never
        rewritten into "no materials".
        """
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        case_id = case_id.strip()
        if status is not None:
            if not isinstance(status, str):
                raise TypeError("status must be a string or None")
            if status not in _LIFECYCLE_STATUSES:
                allowed = ", ".join(sorted(_LIFECYCLE_STATUSES))
                raise ValueError(
                    f"status must be one of: {allowed}"
                )
        row_limit = _validated_limit(limit)
        if self._material_journeys is None:
            raise RuntimeError(
                "the injected facade does not provide material_journeys() "
                "for the case-materials linkage"
            )
        await self._api.case_overview(case_id)
        views = await self._material_journeys(
            case_id=case_id, status=status
        )
        # The facade already orders most recent outcome first; the limit is
        # a presentation cap over that order and never re-sorts.
        rows = [material_row(view) for view in tuple(views)]
        if row_limit is not None:
            rows = rows[:row_limit]
        return {"case_id": case_id, "materials": rows, "count": len(rows)}

    async def load_case_reports(
        self,
        case_id: str,
        *,
        limit: int | None = CASE_LINK_LIMIT,
    ) -> dict[str, Any]:
        """Load the selected case's report versions, newest first (Phase C).

        Reads only ``PrismAPI.report_versions`` for the case (creation-order
        ledger) and reverses it by ``created_at`` — a presentation-layer
        sort over immutable saved versions.  Every row carries the deep link
        to its ``/reports/{version_id}`` detail page.  Validation and the
        empty-vs-failure distinction follow :meth:`load_case_materials`.
        """
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        case_id = case_id.strip()
        row_limit = _validated_limit(limit)
        if self._report_versions is None:
            raise RuntimeError(
                "the injected facade does not provide report_versions() "
                "for the case-reports linkage"
            )
        await self._api.case_overview(case_id)
        versions = tuple(await self._report_versions(case_id=case_id))

        def _created(index: int) -> datetime:
            value = getattr(versions[index], "created_at", None)
            return value if value is not None else _MIN_INSTANT

        order = sorted(
            range(len(versions)),
            key=lambda index: (_created(index), index),
            reverse=True,
        )
        rows = []
        for index in order:
            row = report_row(versions[index])
            row["detail_url"] = report_detail_url(row["version_id"])
            rows.append(row)
        if row_limit is not None:
            rows = rows[:row_limit]
        return {"case_id": case_id, "reports": rows, "count": len(rows)}

    @staticmethod
    def _normalize_kinds(
        kinds: Iterable[str] | None,
    ) -> tuple[str, ...] | None:
        if kinds is None:
            return None
        if isinstance(kinds, str):
            raise TypeError(
                "kinds must be an iterable of entry kinds, not a string"
            )
        items = tuple(kinds)
        if not items:
            return None
        for kind in items:
            if kind not in ENTRY_KINDS:
                allowed = ", ".join(sorted(ENTRY_KINDS))
                raise ValueError(
                    f"unknown snapshot kind {kind!r}; must be one of: {allowed}"
                )
        return items


__all__ = ["CaseHomeController", "PrismFacade", "parse_as_of", "snapshot_view"]
