"""Deterministic evolution analysis over historical graph timelines (FR-4).

The analyzer is a pure projection layer.  It consumes ``GraphTimeline``
snapshots from an injected graph service and derives staged timelines,
turning points, change reasons, evidence gaps and open questions without any
network or LLM access.  Derivations use only evidence the timeline itself
records — node types, ``invalid_at`` supersessions, claim ``revised_by``
links, stances and source bindings — and where that evidence is missing the
service reports an explicit :class:`~prism.analyzer.EvidenceGap` instead of
inventing a conclusion.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any, Protocol

from prism.graph import GraphTimeline, TimelineEntry

from .models import (
    CLAIM_REVISED,
    ENTRY_KINDS,
    FACT_CHANGE,
    FACT_SUPERSEDED,
    GAP_EMPTY_TIMELINE,
    GAP_MISSING_CASE_DEFINITION,
    GAP_MISSING_EVIDENCE_LOCATION,
    GAP_MISSING_PRIMARY_SOURCE,
    GAP_UNATTRIBUTED_ENTRY,
    INTERPRETATION_CHANGE,
    ORIGIN_OPEN_QUESTION_NODE,
    ORIGIN_UNCONFIRMED_CHANGE_CAUSE,
    ORIGIN_UNCERTAIN_CLAIM,
    REASON_NODE_TYPES,
    SUBSTANTIVE_KINDS,
    TURNING_NODE_TYPES,
    ChangeReason,
    ComparisonChange,
    EvidenceGap,
    EvolutionAnalysis,
    EvolutionComparison,
    HistoricalCaseState,
    OpenQuestion,
    TimelineStage,
    TurningPoint,
    layer_for_kind,
    require_aware,
    require_stage,
    require_text,
    stage_matches,
)


class _GraphReader(Protocol):
    async def timeline(self, case_id: str, as_of: datetime) -> GraphTimeline: ...


_Clock = Callable[[], datetime]


class _Staged:
    """Internal pairing of an entry with its stage projection and payload."""

    __slots__ = ("entry", "stage", "payload")

    def __init__(
        self, entry: TimelineEntry, stage: TimelineStage, payload: dict[str, Any]
    ) -> None:
        self.entry = entry
        self.stage = stage
        self.payload = payload


def _required_dependency(name: str, dependency: object, method: str):
    if dependency is None:
        raise ValueError(f"{name} is required")
    if not callable(getattr(dependency, method, None)):
        raise TypeError(f"{name} must provide {method}()")
    return dependency


class AnalyzerService:
    """Analyze case evolution from historical graph timelines.

    All outputs are frozen, tuple-based and deterministically ordered for any
    input ordering: entries sort by ``(valid_at, reference_time, kind,
    episode_key)``, and every derived collection carries a total sort key.
    """

    def __init__(self, graph_service: _GraphReader, *, clock: _Clock | None = None):
        self._graph = _required_dependency("graph_service", graph_service, "timeline")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._clock: _Clock = clock or (lambda: datetime.now(timezone.utc))

    async def analyze(
        self,
        case_id: str,
        as_of: datetime | None = None,
        *,
        kinds: Iterable[str] | None = None,
    ) -> EvolutionAnalysis:
        """Return the staged timeline and derived signals valid at ``as_of``.

        ``as_of=None`` uses the injected clock (timezone-aware "now").
        ``kinds`` optionally restricts the whole analysis to a subset of
        entry kinds, e.g. ``("claim",)`` for an opinions-only view (FR-4.8).
        """

        require_text("case_id", case_id)
        kind_filter = self._kind_filter(kinds)
        if as_of is None:
            as_of = self._now()
        else:
            require_aware("as_of", as_of)

        timeline = await self._fetch(case_id, as_of)
        case_type, case_status = self._case_metadata(timeline.entries, as_of)
        staged = self._staged(self._visible(timeline, kind_filter))
        invalidated = self._staged(
            self._visible_entries(timeline.invalidated_entries, kind_filter)
        )

        return EvolutionAnalysis(
            case_id=case_id,
            as_of=as_of,
            case_type=case_type,
            stages=tuple(item.stage for item in staged),
            turning_points=self._turning_points(staged, invalidated, as_of),
            change_reasons=self._change_reasons(staged),
            evidence_gaps=self._evidence_gaps(case_id, as_of, staged, kind_filter),
            open_questions=self._open_questions(staged, invalidated),
            case_status=case_status,
            invalidated_stages=tuple(item.stage for item in invalidated),
        )

    async def state(
        self, case_id: str, cutoff_at: datetime
    ) -> HistoricalCaseState:
        """Return the auditable node/fact/interpretation state at a cutoff."""

        require_text("case_id", case_id)
        require_aware("cutoff_at", cutoff_at)
        analysis = await self.analyze(case_id, cutoff_at)
        return HistoricalCaseState(
            case_id=case_id,
            cutoff_at=cutoff_at,
            case_type=analysis.case_type,
            status=analysis.case_status,
            nodes=tuple(
                stage for stage in analysis.stages if stage.kind == "evolution_node"
            ),
            facts=tuple(
                stage for stage in analysis.stages if stage.kind == "temporal_fact"
            ),
            interpretations=tuple(
                stage for stage in analysis.stages if stage.kind == "claim"
            ),
            evidence_gaps=analysis.evidence_gaps,
            invalidated_facts=tuple(
                stage
                for stage in analysis.invalidated_stages
                if stage.kind == "temporal_fact"
            ),
            relations=tuple(
                stage
                for stage in analysis.stages
                if stage.kind == "temporal_relation"
            ),
        )

    async def snapshot(
        self,
        case_id: str,
        as_of: datetime,
        *,
        stage: str | None = None,
        kinds: Iterable[str] | None = None,
    ) -> HistoricalCaseState:
        """Return the formal GTI-backed historical snapshot valid at ``as_of``.

        The snapshot is the auditable, future-free state contract (FR-3.6,
        FR-4.2): the nodes, facts, claims (interpretation stages), relations
        and evidence gaps effective at ``as_of``, plus the facts invalidated
        by that instant — all projected from the graph service's timeline,
        never stored anywhere else.  It fail-closes on any reader that
        violates the contract: an entry known only after ``as_of``
        (reference/publication time later than the cutoff), an entry listed
        as effective outside its ``[valid_at, invalid_at)`` window, or a
        still-valid entry listed as invalidated raises ``ValueError`` naming
        the offending episode.

        ``stage`` restricts the effective buckets to one deterministic,
        recorded stage (see :data:`prism.analyzer.STAGES`): membership is a
        pure lookup over ``evolution_node.node_type`` / ``claim.stance``, so
        the LLM never decides membership, no stage is invented, and entries
        keep the layer their kind implies.  ``kinds`` is the existing
        entry-kind filter.  Evidence gaps describe the case at ``as_of``
        (computed on the unfiltered, kind-filtered projection), so a narrow
        stage never manufactures an ``empty_timeline`` gap for a case that
        has evidence outside the stage.
        """
        require_text("case_id", case_id)
        require_aware("as_of", as_of)
        require_stage(stage)
        kind_filter = self._kind_filter(kinds)

        timeline = await self._fetch(case_id, as_of)
        self._assert_snapshot_boundaries(timeline, as_of)
        case_type, case_status = self._case_metadata(timeline.entries, as_of)
        visible = self._visible(timeline, kind_filter)
        invalidated = self._visible_entries(timeline.invalidated_entries, kind_filter)
        staged = self._staged(visible)
        invalidated_staged = self._staged(invalidated)
        evidence_gaps = self._evidence_gaps(case_id, as_of, staged, kind_filter)
        if stage is not None:
            staged = tuple(
                item for item in staged if stage_matches(stage, item.stage)
            )
            invalidated_staged = tuple(
                item
                for item in invalidated_staged
                if stage_matches(stage, item.stage)
            )
        return HistoricalCaseState(
            case_id=case_id,
            cutoff_at=as_of,
            case_type=case_type,
            status=case_status,
            nodes=tuple(
                item.stage for item in staged if item.stage.kind == "evolution_node"
            ),
            facts=tuple(
                item.stage for item in staged if item.stage.kind == "temporal_fact"
            ),
            interpretations=tuple(
                item.stage for item in staged if item.stage.kind == "claim"
            ),
            evidence_gaps=evidence_gaps,
            invalidated_facts=tuple(
                item.stage
                for item in invalidated_staged
                if item.stage.kind == "temporal_fact"
            ),
            relations=tuple(
                item.stage for item in staged if item.stage.kind == "temporal_relation"
            ),
        )

    @staticmethod
    def _assert_snapshot_boundaries(
        timeline: GraphTimeline, as_of: datetime
    ) -> None:
        """Fail closed when a graph reader violates the snapshot contract.

        ``GraphService.timeline`` already enforces the knowledge boundary
        (``reference_time <= as_of``) and the half-open effectiveness window;
        a historical snapshot re-verifies both on every entry so no reader
        bug can silently leak future reference/publication evidence into a
        historical state (FR-3.6, FR-4.2, U2).
        """
        for entry in timeline.entries:
            if entry.reference_time > as_of:
                raise ValueError(
                    f"timeline entry {entry.episode_key!r} (kind {entry.kind}) "
                    f"is known only at {entry.reference_time.isoformat()}, "
                    f"after as_of {as_of.isoformat()}: future "
                    "reference/publication evidence must never enter a "
                    "historical snapshot"
                )
            if entry.valid_at > as_of or (
                entry.invalid_at is not None and entry.invalid_at <= as_of
            ):
                raise ValueError(
                    f"timeline entry {entry.episode_key!r} (kind {entry.kind}) "
                    f"is listed as effective at {as_of.isoformat()} outside "
                    "its [valid_at, invalid_at) window"
                )
        for entry in timeline.invalidated_entries:
            if entry.reference_time > as_of:
                raise ValueError(
                    f"timeline entry {entry.episode_key!r} (kind {entry.kind}) "
                    f"is known only at {entry.reference_time.isoformat()}, "
                    f"after as_of {as_of.isoformat()}: future "
                    "reference/publication evidence must never enter a "
                    "historical snapshot"
                )
            if entry.invalid_at is None or entry.invalid_at > as_of:
                raise ValueError(
                    f"timeline entry {entry.episode_key!r} (kind {entry.kind}) "
                    f"is listed as invalidated although its invalid_at "
                    f"({entry.invalid_at.isoformat() if entry.invalid_at is not None else 'unset'}) "
                    f"is not at or before as_of {as_of.isoformat()}"
                )

    async def compare(
        self,
        case_id: str,
        earlier: datetime,
        later: datetime,
        *,
        kinds: Iterable[str] | None = None,
    ) -> EvolutionComparison:
        """Compare effective entries at two historical instants.

        Effectiveness follows the graph contract ``[valid_at, invalid_at)``:
        an entry counts at an instant when ``valid_at <= instant`` and either
        ``invalid_at`` is unset or ``instant < invalid_at``.  Each change is
        layer-classified so fact changes stay separable from interpretation
        changes (FR-4.4).
        """

        require_text("case_id", case_id)
        require_aware("earlier", earlier)
        require_aware("later", later)
        if later < earlier:
            raise ValueError("later must not be earlier than earlier")
        kind_filter = self._kind_filter(kinds)

        earlier_map = {
            entry.episode_key: entry
            for entry in self._visible(await self._fetch(case_id, earlier), kind_filter)
        }
        later_map = {
            entry.episode_key: entry
            for entry in self._visible(await self._fetch(case_id, later), kind_filter)
        }

        return EvolutionComparison(
            case_id=case_id,
            earlier=earlier,
            later=later,
            added=self._changes(
                later_map[key] for key in later_map.keys() - earlier_map.keys()
            ),
            removed=self._changes(
                earlier_map[key] for key in earlier_map.keys() - later_map.keys()
            ),
            unchanged=self._changes(
                later_map[key] for key in earlier_map.keys() & later_map.keys()
            ),
        )

    async def _fetch(self, case_id: str, as_of: datetime) -> GraphTimeline:
        timeline = await self._graph.timeline(case_id, as_of)
        if not isinstance(timeline, GraphTimeline):
            raise TypeError(
                "graph_service must return a GraphTimeline, got "
                f"{type(timeline).__name__}"
            )
        if timeline.case_id != case_id:
            raise ValueError(
                f"timeline case_id {timeline.case_id!r} does not match "
                f"requested case_id {case_id!r}"
            )
        if timeline.as_of != as_of:
            raise ValueError(
                f"timeline as_of {timeline.as_of!r} does not match requested as_of {as_of!r}"
            )
        return timeline

    @staticmethod
    def _visible(
        timeline: GraphTimeline, kind_filter: frozenset[str] | None
    ) -> tuple[TimelineEntry, ...]:
        return AnalyzerService._visible_entries(timeline.entries, kind_filter)

    @staticmethod
    def _visible_entries(
        entries: tuple[TimelineEntry, ...],
        kind_filter: frozenset[str] | None,
    ) -> tuple[TimelineEntry, ...]:
        if kind_filter is not None:
            entries = tuple(entry for entry in entries if entry.kind in kind_filter)
        return tuple(
            sorted(
                entries,
                key=lambda entry: (
                    entry.valid_at,
                    entry.reference_time,
                    entry.kind,
                    entry.episode_key,
                ),
            )
        )

    @classmethod
    def _staged(cls, entries: Iterable[TimelineEntry]) -> tuple[_Staged, ...]:
        return tuple(cls._stage(entry) for entry in entries)

    @staticmethod
    def _stage(entry: TimelineEntry) -> _Staged:
        payload = AnalyzerService._payload(entry)
        node_type = None
        if entry.kind == "evolution_node":
            candidate = payload.get("node_type")
            if isinstance(candidate, str) and candidate.strip():
                node_type = candidate
        claim_type = None
        if entry.kind == "claim":
            candidate = payload.get("claim_type")
            if isinstance(candidate, str) and candidate.strip():
                claim_type = candidate
        relation_type = None
        source_ref = None
        target_ref = None
        if entry.kind == "temporal_relation":
            relation_type = AnalyzerService._payload_text(payload, "relation_type")
            source_ref = AnalyzerService._payload_text(payload, "source_ref")
            target_ref = AnalyzerService._payload_text(payload, "target_ref")
        record_fields = {
            "evolution_node": "node_id",
            "temporal_fact": "fact_id",
            "claim": "claim_id",
            "temporal_relation": "relation_id",
        }
        record_id = AnalyzerService._payload_text(
            payload, record_fields.get(entry.kind, "")
        )
        happened_at = None
        happened_value = payload.get("happened_at")
        if isinstance(happened_value, str):
            try:
                happened_at = datetime.fromisoformat(happened_value)
            except ValueError:
                happened_at = None
        stage = TimelineStage(
            episode_key=entry.episode_key,
            kind=entry.kind,
            layer=layer_for_kind(entry.kind),
            summary=entry.summary,
            valid_at=entry.valid_at,
            invalid_at=entry.invalid_at,
            reference_time=entry.reference_time,
            source_ids=entry.source_ids,
            node_type=node_type,
            confidence=entry.confidence,
            provenance_type=entry.provenance_type,
            stance=entry.stance,
            happened_at=happened_at,
            evidence=entry.evidence,
            claim_type=claim_type,
            relation_type=relation_type,
            source_ref=source_ref,
            target_ref=target_ref,
            record_id=record_id,
            evidence_role=entry.evidence_role,
            cited_source_ref=entry.cited_source_ref,
        )
        return _Staged(entry, stage, payload)

    @staticmethod
    def _turning_points(
        staged: tuple[_Staged, ...],
        invalidated: tuple[_Staged, ...] = (),
        as_of: datetime | None = None,
    ) -> tuple[TurningPoint, ...]:
        points: list[TurningPoint] = []
        for item in staged:
            stage = item.stage
            if stage.kind == "evolution_node" and stage.node_type in TURNING_NODE_TYPES:
                points.append(
                    TurningPoint(
                        stage.episode_key,
                        stage.node_type,
                        stage.valid_at,
                        stage.summary,
                        stage.source_ids,
                        stage.evidence,
                    )
                )
            elif stage.kind == "temporal_fact" and stage.invalid_at is not None:
                if (
                    item.payload.get("schema") == "prism.graph.episode.v2"
                    and as_of is not None
                    and stage.invalid_at > as_of
                ):
                    continue
                points.append(
                    TurningPoint(
                        stage.episode_key,
                        FACT_SUPERSEDED,
                        stage.invalid_at,
                        stage.summary,
                        stage.source_ids,
                        stage.evidence,
                    )
                )
            elif (
                stage.kind == "temporal_relation"
                and stage.relation_type in {"supersedes", "revises"}
            ):
                points.append(
                    TurningPoint(
                        stage.episode_key,
                        stage.relation_type,
                        stage.valid_at,
                        stage.summary,
                        stage.source_ids,
                        stage.evidence,
                    )
                )
        for item in invalidated:
            stage = item.stage
            if stage.kind == "temporal_fact" and stage.invalid_at is not None:
                points.append(
                    TurningPoint(
                        stage.episode_key,
                        FACT_SUPERSEDED,
                        stage.invalid_at,
                        stage.summary,
                        stage.source_ids,
                        stage.evidence,
                    )
                )
        return tuple(
            sorted(points, key=lambda point: (point.at, point.category, point.episode_key))
        )

    @classmethod
    def _change_reasons(cls, staged: tuple[_Staged, ...]) -> tuple[ChangeReason, ...]:
        reasons: list[ChangeReason] = []
        for item in staged:
            stage = item.stage
            is_v2 = item.payload.get("schema") == "prism.graph.episode.v2"
            if (
                stage.kind == "temporal_relation"
                and stage.relation_type == "triggered_by"
                and stage.evidence
                and stage.source_ref is not None
                and stage.target_ref is not None
            ):
                reasons.append(
                    ChangeReason(
                        stage.episode_key,
                        "triggered_by",
                        FACT_CHANGE,
                        stage.valid_at,
                        f"{stage.source_ref} was explicitly triggered by "
                        f"{stage.target_ref}.",
                        stage.source_ids,
                        stage.evidence,
                    )
                )
                continue
            if stage.kind == "evolution_node" and stage.node_type in REASON_NODE_TYPES:
                reason_summary = cls._payload_text(item.payload, "change_reason")
                # v2 does not infer causality from a node type or summary.
                # Legacy episodes retain their historical projection behavior.
                if (
                    reason_summary is None
                    and is_v2
                ):
                    continue
                if is_v2 and not stage.evidence:
                    continue
                reasons.append(
                    ChangeReason(
                        stage.episode_key,
                        f"node:{stage.node_type}",
                        FACT_CHANGE,
                        stage.valid_at,
                        reason_summary or stage.summary,
                        stage.source_ids,
                    )
                )
            elif stage.kind == "temporal_fact" and stage.invalid_at is not None:
                if is_v2:
                    continue
                reasons.append(
                    ChangeReason(
                        stage.episode_key,
                        FACT_SUPERSEDED,
                        FACT_CHANGE,
                        stage.invalid_at,
                        f"{stage.summary} ceased to be valid at "
                        f"{stage.invalid_at.isoformat()}.",
                        stage.source_ids,
                    )
                )
            elif stage.kind == "claim":
                revised_by = cls._payload_text(item.payload, "revised_by")
                if revised_by is not None and not is_v2:
                    claim_id = cls._payload_text(item.payload, "claim_id")
                    reasons.append(
                        ChangeReason(
                            stage.episode_key,
                            CLAIM_REVISED,
                            INTERPRETATION_CHANGE,
                            stage.valid_at,
                            f"Claim {claim_id or stage.episode_key} was revised "
                            f"by {revised_by}.",
                            stage.source_ids,
                        )
                    )
        return tuple(
            sorted(reasons, key=lambda reason: (reason.at, reason.reason_type, reason.episode_key))
        )

    @staticmethod
    def _evidence_gaps(
        case_id: str,
        as_of: datetime,
        staged: tuple[_Staged, ...],
        kind_filter: frozenset[str] | None,
    ) -> tuple[EvidenceGap, ...]:
        gaps: list[EvidenceGap] = []
        if not staged:
            if kind_filter is None:
                detail = f"no timeline entries for case {case_id!r} at {as_of.isoformat()}"
            else:
                kinds_label = "/".join(sorted(kind_filter))
                detail = (
                    f"no {kinds_label} entries for case {case_id!r} "
                    f"at {as_of.isoformat()}"
                )
            return (EvidenceGap(GAP_EMPTY_TIMELINE, detail),)
        # Structural gaps describe the unfiltered timeline; a kind filter that
        # deliberately excludes them must not manufacture a misleading gap.
        if kind_filter is None and not any(
            item.stage.kind == "evolution_case" for item in staged
        ):
            gaps.append(
                EvidenceGap(
                    GAP_MISSING_CASE_DEFINITION,
                    "timeline carries no evolution_case entry for the requested case",
                )
            )
        for item in staged:
            stage = item.stage
            if stage.kind in SUBSTANTIVE_KINDS and not item.entry.source_ids:
                gaps.append(
                    EvidenceGap(
                        GAP_UNATTRIBUTED_ENTRY,
                        f"{stage.kind} entry {stage.episode_key!r} has no source_ids",
                        stage.episode_key,
                    )
                )
            elif (
                stage.kind in SUBSTANTIVE_KINDS
                and "evidence" in item.payload
                and not item.entry.evidence
            ):
                gaps.append(
                    EvidenceGap(
                        GAP_MISSING_EVIDENCE_LOCATION,
                        f"{stage.kind} entry {stage.episode_key!r} has source_ids "
                        "but no corpus paragraph/page or excerpt",
                        stage.episode_key,
                        stage.source_ids,
                    )
                )
            if stage.evidence_role == "cited_prior_research":
                reference = (
                    f"; cited reference: {stage.cited_source_ref}"
                    if stage.cited_source_ref
                    else ""
                )
                gaps.append(
                    EvidenceGap(
                        GAP_MISSING_PRIMARY_SOURCE,
                        f"{stage.kind} entry {stage.episode_key!r} is secondary "
                        f"evidence from a review; no original material is linked"
                        f"{reference}",
                        stage.episode_key,
                        stage.source_ids,
                    )
                )
        return tuple(
            sorted(gaps, key=lambda gap: (gap.gap_type, gap.episode_key or ""))
        )

    @classmethod
    def _open_questions(
        cls,
        staged: tuple[_Staged, ...],
        invalidated: tuple[_Staged, ...] = (),
    ) -> tuple[OpenQuestion, ...]:
        questions: list[OpenQuestion] = []
        for item in staged:
            stage = item.stage
            if stage.kind == "evolution_node" and stage.node_type == "open_question":
                questions.append(
                    OpenQuestion(
                        stage.episode_key,
                        ORIGIN_OPEN_QUESTION_NODE,
                        stage.summary,
                        None,
                        stage.valid_at,
                        stage.source_ids,
                    )
                )
            elif stage.kind == "claim" and stage.stance == "uncertain":
                questions.append(
                    OpenQuestion(
                        stage.episode_key,
                        ORIGIN_UNCERTAIN_CLAIM,
                        stage.summary,
                        cls._payload_text(item.payload, "actor"),
                        stage.valid_at,
                        stage.source_ids,
                    )
                )
        triggered_refs = {
            item.stage.source_ref
            for item in staged
            if item.stage.kind == "temporal_relation"
            and item.stage.relation_type == "triggered_by"
            and item.stage.evidence
            and item.stage.source_ref is not None
        }
        superseded_targets = {
            item.stage.target_ref
            for item in staged
            if item.stage.kind == "temporal_relation"
            and item.stage.relation_type == "supersedes"
            and item.stage.target_ref is not None
        }
        change_markers: list[tuple[_Staged, str]] = []
        for item in staged:
            stage = item.stage
            if item.payload.get("schema") != "prism.graph.episode.v2":
                continue
            if (
                stage.kind == "temporal_relation"
                and stage.relation_type in {"supersedes", "revises"}
                and stage.source_ref is not None
            ):
                change_markers.append((item, stage.source_ref))
            elif (
                stage.kind == "evolution_node"
                and stage.node_type in {"revision", "reversal", "replacement"}
            ):
                reference = cls._payload_text(item.payload, "node_id")
                if reference is not None:
                    change_markers.append((item, reference))
        for item in invalidated:
            if item.stage.kind != "temporal_fact":
                continue
            fact_id = cls._payload_text(item.payload, "fact_id")
            if fact_id is not None and fact_id not in superseded_targets:
                change_markers.append((item, fact_id))
        for item, reference in change_markers:
            stage = item.stage
            if reference in triggered_refs:
                continue
            questions.append(
                OpenQuestion(
                    stage.episode_key,
                    ORIGIN_UNCONFIRMED_CHANGE_CAUSE,
                    f"The cause of change {reference!r} is unconfirmed; no "
                    "evidence-backed triggered_by relation is recorded.",
                    None,
                    stage.invalid_at or stage.valid_at,
                    stage.source_ids,
                )
            )
        return tuple(
            sorted(
                questions,
                key=lambda question: (question.at, question.origin, question.episode_key),
            )
        )

    @classmethod
    def _changes(cls, entries: Iterable[TimelineEntry]) -> tuple[ComparisonChange, ...]:
        changes = [
            ComparisonChange(
                episode_key=entry.episode_key,
                kind=entry.kind,
                layer=layer_for_kind(entry.kind),
                summary=entry.summary,
                valid_at=entry.valid_at,
                invalid_at=entry.invalid_at,
                source_ids=entry.source_ids,
                confidence=entry.confidence,
                provenance_type=entry.provenance_type,
                stance=entry.stance,
                evidence=entry.evidence,
                evidence_role=entry.evidence_role,
                cited_source_ref=entry.cited_source_ref,
            )
            for entry in entries
        ]
        return tuple(
            sorted(changes, key=lambda change: (change.valid_at, change.kind, change.episode_key))
        )

    @staticmethod
    def _case_metadata(
        entries: tuple[TimelineEntry, ...], as_of: datetime
    ) -> tuple[str | None, str | None]:
        case_type = None
        status = None
        for entry in entries:
            if entry.kind == "evolution_case":
                payload = AnalyzerService._payload(entry)
                type_value = payload.get("case_type")
                if case_type is None and isinstance(type_value, str) and type_value.strip():
                    case_type = type_value
                status_value = payload.get("status")
                status_at = AnalyzerService._payload_datetime(
                    payload, "status_at", entry.valid_at
                )
                observed_at = AnalyzerService._payload_datetime(
                    payload, "status_observed_at", entry.reference_time
                )
                if (
                    isinstance(status_value, str)
                    and status_value.strip()
                    and status_at <= as_of
                    and observed_at <= as_of
                ):
                    status = status_value
        return case_type, status

    @staticmethod
    def _kind_filter(kinds: Iterable[str] | None) -> frozenset[str] | None:
        if kinds is None:
            return None
        if isinstance(kinds, str):
            raise TypeError("kinds must be an iterable of strings, not a string")
        selected = tuple(kinds)
        if not selected:
            raise ValueError("kinds must not be empty")
        for value in selected:
            require_text("kinds", value)
            if value not in ENTRY_KINDS:
                allowed = ", ".join(sorted(ENTRY_KINDS))
                raise ValueError(f"kinds must be one of: {allowed}; got {value!r}")
        return frozenset(selected)

    @staticmethod
    def _payload(entry: TimelineEntry) -> dict[str, Any]:
        try:
            parsed = json.loads(entry.payload)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _payload_text(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        return None

    @staticmethod
    def _payload_datetime(
        payload: dict[str, Any], key: str, fallback: datetime
    ) -> datetime:
        value = payload.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    return parsed
            except ValueError:
                pass
        return fallback

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise RuntimeError("clock must return timezone-aware datetimes")
        return value
