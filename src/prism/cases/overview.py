"""Project-owned, graph-independent overview of accumulated evolution cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from prism.cases.ledger import CaseExtractionLedger, CaseLedgerEntry
from prism.domain import Material

SortField = Literal["case_id", "last_updated", "latest_observed"]

_SORT_FIELDS = frozenset({"case_id", "last_updated", "latest_observed"})


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _optional_filter(name: str, value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} must be a non-empty string when supplied")


@dataclass(frozen=True, slots=True)
class CaseOverview:
    """A compact, restart-durable summary of one accumulated case."""

    case_id: str
    case_type: str
    name: str
    status: str
    material_count: int
    earliest_observed_at: datetime
    latest_observed_at: datetime
    latest_node_at: datetime
    last_updated_at: datetime
    has_unresolved_gaps: bool
    has_unresolved_conflicts: bool

    def __post_init__(self) -> None:
        for name in ("case_id", "case_type", "name", "status"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.material_count, bool) or not isinstance(
            self.material_count, int
        ):
            raise TypeError("material_count must be an integer")
        if self.material_count < 0:
            raise ValueError("material_count must not be negative")
        for name in (
            "earliest_observed_at",
            "latest_observed_at",
            "latest_node_at",
            "last_updated_at",
        ):
            _require_aware(name, getattr(self, name))
        if self.earliest_observed_at > self.latest_observed_at:
            raise ValueError("earliest_observed_at must not exceed latest_observed_at")
        if self.latest_node_at > self.latest_observed_at:
            raise ValueError("latest_node_at must not exceed latest_observed_at")
        if self.last_updated_at < self.latest_observed_at:
            raise ValueError("last_updated_at must not precede latest_observed_at")
        for name in (
            "has_unresolved_gaps",
            "has_unresolved_conflicts",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")


class CaseOverviewService:
    """Build case overviews from the durable extraction ledger only."""

    def __init__(self, ledger: CaseExtractionLedger) -> None:
        if not isinstance(ledger, CaseExtractionLedger):
            raise TypeError("ledger must be a CaseExtractionLedger")
        self._ledger = ledger

    def list(
        self,
        *,
        case_id: str | None = None,
        case_type: str | None = None,
        status: str | None = None,
        unresolved_only: bool = False,
        order: SortField = "case_id",
        reverse: bool = False,
    ) -> tuple[CaseOverview, ...]:
        """Return a stable, filtered overview of every known case."""

        _optional_filter("case_id", case_id)
        _optional_filter("case_type", case_type)
        _optional_filter("status", status)
        if not isinstance(unresolved_only, bool):
            raise TypeError("unresolved_only must be a boolean")
        if order not in _SORT_FIELDS:
            allowed = ", ".join(sorted(_SORT_FIELDS))
            raise ValueError(f"order must be one of: {allowed}")
        if not isinstance(reverse, bool):
            raise TypeError("reverse must be a boolean")

        overviews = [
            self._overview(case_key)
            for case_key in self._ledger.case_ids()
            if (
                case_id is None
                or case_key == case_id
            )
        ]
        filtered = [
            item
            for item in overviews
            if (
                (case_type is None or item.case_type == case_type)
                and (status is None or item.status == status)
                and (
                    not unresolved_only
                    or item.has_unresolved_gaps
                    or item.has_unresolved_conflicts
                )
            )
        ]
        if order == "case_id":
            key = lambda item: item.case_id
        elif order == "last_updated":
            key = lambda item: (item.last_updated_at, item.case_id)
        else:
            key = lambda item: (item.latest_observed_at, item.case_id)
        return tuple(sorted(filtered, key=key, reverse=reverse))

    def get(self, case_id: str) -> CaseOverview:
        """Return one known case, or raise ``LookupError``."""

        _optional_filter("case_id", case_id)
        items = self.list(case_id=case_id)
        if not items:
            raise LookupError(f"no accumulated evolution case {case_id!r}")
        return items[0]

    def _overview(self, case_id: str) -> CaseOverview:
        entries = self._ledger.entries(case_id)
        if not entries:
            raise LookupError(f"no accumulated evolution case {case_id!r}")
        template = entries[0].extraction.case
        if template is None or template.case_id != case_id:
            raise ValueError(
                f"ledger entry for case {case_id!r} does not declare that case"
            )

        observed = [
            timestamp
            for entry in entries
            for timestamp in self._observed_timestamps(entry.material, entry)
        ]
        nodes = [
            timestamp
            for entry in entries
            for node in entry.extraction.nodes
            for timestamp in (node.happened_at, node.valid_at)
            if timestamp is not None
        ]
        if not observed:
            observed = [template.start_at]
        if not nodes:
            nodes = [template.start_at]
        latest_observed = max(observed)
        last_updated = self._ledger.case_updated_at(case_id)
        if last_updated is None:
            raise LookupError(f"no accumulated evolution case {case_id!r}")
        return CaseOverview(
            case_id=case_id,
            case_type=template.case_type,
            name=template.canonical_name,
            status=template.status,
            material_count=len(entries),
            earliest_observed_at=min(observed),
            latest_observed_at=latest_observed,
            latest_node_at=max(nodes),
            last_updated_at=last_updated,
            has_unresolved_gaps=any(
                entry.extraction.evidence_gaps for entry in entries
            ),
            has_unresolved_conflicts=any(
                entry.extraction.conflicts for entry in entries
            ),
        )

    @staticmethod
    def _observed_timestamps(
        material: Material, entry: CaseLedgerEntry
    ) -> tuple[datetime, ...]:
        extraction = entry.extraction
        timestamps = [material.fetched_at]
        timestamps.extend(
            value
            for value in (
                getattr(node, "observed_at", None)
                for node in extraction.nodes
            )
            if value is not None
        )
        timestamps.extend(
            value
            for value in (
                getattr(fact, "observed_at", None)
                for fact in extraction.temporal_facts
            )
            if value is not None
        )
        timestamps.extend(
            value
            for value in (
                getattr(claim, "observed_at", None)
                for claim in extraction.claims
            )
            if value is not None
        )
        timestamps.extend(
            value
            for value in (
                getattr(relation, "observed_at", None)
                for relation in extraction.relations
            )
            if value is not None
        )
        timestamps.extend(
            value
            for value in (
                getattr(conflict, "observed_at", None)
                for conflict in extraction.conflicts
            )
            if value is not None
        )
        return tuple(timestamps)


__all__ = ["CaseOverview", "CaseOverviewService"]
