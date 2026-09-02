"""Domain-to-episode mapping and offline historical timeline queries."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
)

from .backend import GraphBackend
from .models import (
    GraphEpisode,
    GraphTimeline,
    GraphWriteResult,
    TimelineEntry,
    require_aware,
    require_text,
)


SCHEMA = "prism.graph.episode.v2"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evidence_payload(evidence: tuple[EvidenceLocator, ...]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": item.source_id,
            "corpus_path": item.corpus_path,
            "paragraph": item.paragraph,
            "page": item.page,
            "quote": item.quote,
        }
        for item in evidence
    ]


class GraphService:
    """Submit explicit graph episodes and evaluate their temporal validity."""

    def __init__(self, backend: GraphBackend):
        if backend is None:
            raise ValueError("backend is required")
        self.backend = backend

    async def add_case(
        self,
        case: EvolutionCase,
        *,
        nodes: Iterable[EvolutionNode] = (),
        facts: Iterable[TemporalFact] = (),
        claims: Iterable[Claim] = (),
        materials: Iterable[Material] = (),
    ) -> GraphWriteResult:
        """Incrementally add a case and its source-backed evolution records.

        When materials are supplied, every node/fact/claim episode is bounded
        by the availability of its bound materials: an entry can never be
        treated as observed before every material it cites was published, so a
        record kept only by a later-published source cannot leak into earlier
        historical states.  Multi-source entries use the latest bound-material
        publication time (the latest necessary observation time), never the
        earliest.
        """
        material_list = tuple(materials)
        availability: dict[str, datetime] | None = None
        if material_list:
            availability = {
                material.id: material.published_at for material in material_list
            }
        episodes = [self._case_episode(case)]
        for node in nodes:
            if node.case_id != case.case_id:
                raise ValueError(
                    f"node {node.id!r} belongs to {node.case_id!r}, not {case.case_id!r}"
                )
            episodes.append(self._node_episode(node, availability))
        episodes.extend(
            self._fact_episode(case.case_id, fact, availability) for fact in facts
        )
        episodes.extend(
            self._claim_episode(case.case_id, claim, availability) for claim in claims
        )
        episodes.extend(
            self._material_episode(case.case_id, material) for material in material_list
        )

        added: list[str] = []
        skipped: list[str] = []
        for episode in episodes:
            destination = added if await self.backend.add_episode(episode) else skipped
            destination.append(episode.episode_key)
        return GraphWriteResult(tuple(episodes), tuple(added), tuple(skipped))

    async def timeline(self, case_id: str, as_of: datetime) -> GraphTimeline:
        """Return facts both known and valid at ``as_of``.

        ``reference_time`` is the observation/publication boundary.  Filtering
        it as well as ``valid_at`` prevents a later retrospective source from
        leaking into an earlier historical state.
        """
        require_text("case_id", case_id)
        require_aware("as_of", as_of)
        results = await self.backend.search(f"PRISM timeline for case_id={case_id}")
        entries = [
            self._timeline_entry(episode, as_of=as_of)
            for episode in results
            if episode.case_id == case_id
            and episode.reference_time <= as_of
            and episode.valid_at <= as_of
            and (episode.invalid_at is None or as_of < episode.invalid_at)
        ]
        entries.sort(
            key=lambda entry: (
                entry.valid_at,
                entry.reference_time,
                entry.kind,
                entry.episode_key,
            )
        )
        return GraphTimeline(case_id, as_of, tuple(entries))

    def _make_episode(
        self,
        *,
        case_id: str,
        kind: str,
        identity: str,
        reference_time: datetime,
        valid_at: datetime,
        invalid_at: datetime | None,
        source_ids: tuple[str, ...],
        fields: dict[str, Any],
        confidence: float | None = None,
        provenance_type: str | None = None,
        evidence: tuple[EvidenceLocator, ...] = (),
    ) -> GraphEpisode:
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "case_id": case_id,
            "kind": kind,
            "reference_time": _iso(reference_time),
            "valid_at": _iso(valid_at),
            "invalid_at": _iso(invalid_at),
            "source_ids": list(source_ids),
            "evidence": _evidence_payload(evidence),
            **fields,
        }
        if confidence is not None:
            payload["confidence"] = confidence
        if provenance_type is not None:
            payload["provenance_type"] = provenance_type
        fingerprint = uuid5(NAMESPACE_URL, _json(payload))
        episode_key = str(fingerprint)
        payload["episode_key"] = episode_key
        return GraphEpisode(
            episode_key=episode_key,
            name=f"prism:{case_id}:{kind}:{identity}:{episode_key[:12]}",
            case_id=case_id,
            kind=kind,
            episode_body=_json(payload),
            reference_time=reference_time,
            valid_at=valid_at,
            invalid_at=invalid_at,
            source_ids=source_ids,
            confidence=confidence,
            provenance_type=provenance_type,
            evidence=evidence,
        )

    def _case_episode(self, case: EvolutionCase) -> GraphEpisode:
        return self._make_episode(
            case_id=case.case_id,
            kind="evolution_case",
            identity=case.case_id,
            reference_time=case.start_at,
            valid_at=case.start_at,
            invalid_at=None,
            source_ids=(),
            fields={
                "case_type": case.case_type,
                "canonical_name": case.canonical_name,
                "status": case.status,
                "status_at": _iso(case.status_at or case.start_at),
                "status_observed_at": _iso(
                    case.status_observed_at or case.status_at or case.start_at
                ),
                "node_ids": list(case.node_ids),
            },
        )

    @staticmethod
    def _material_floor(
        availability: dict[str, datetime] | None, source_ids: tuple[str, ...]
    ) -> datetime | None:
        """Latest publication time among bound materials that are available.

        ``None`` when no material availability was supplied or when none of
        the cited source ids resolve; callers keep the recorded observation
        anchor in that case.  Multi-source entries are bounded by the latest
        necessary observation time, never the earliest.
        """
        if not availability:
            return None
        resolved = [
            availability[source_id]
            for source_id in source_ids
            if source_id in availability
        ]
        return max(resolved) if resolved else None

    def _node_episode(
        self,
        node: EvolutionNode,
        availability: dict[str, datetime] | None = None,
    ) -> GraphEpisode:
        valid_at = node.valid_at or node.happened_at
        # The knowledge boundary is never earlier than the bound materials:
        # a node that only a later-published material reports must not appear
        # in states that predate that material.
        observed_at = node.observed_at or node.happened_at
        floor = self._material_floor(availability, node.source_ids)
        if floor is not None and floor > observed_at:
            observed_at = floor
        return self._make_episode(
            case_id=node.case_id,
            kind="evolution_node",
            identity=node.id,
            reference_time=observed_at,
            valid_at=valid_at,
            invalid_at=None,
            source_ids=node.source_ids,
            evidence=node.evidence,
            provenance_type=node.provenance_type,
            fields={
                "node_id": node.id,
                "node_type": node.node_type,
                "happened_at": _iso(node.happened_at),
                "observed_at": _iso(observed_at),
                "summary": node.summary,
                "change_reason": node.change_reason,
                "claim_ids": list(node.claim_ids),
            },
        )

    def _fact_episode(
        self,
        case_id: str,
        fact: TemporalFact,
        availability: dict[str, datetime] | None = None,
    ) -> GraphEpisode:
        identity = f"{fact.subject}:{fact.predicate}:{fact.object}"
        observed_at = fact.observed_at
        floor = self._material_floor(availability, fact.source_ids)
        if floor is not None and floor > observed_at:
            # A fact cannot be treated as observed before the material(s)
            # asserting it were published, however early its recorded
            # observed_at may claim to be.
            observed_at = floor
        return self._make_episode(
            case_id=case_id,
            kind="temporal_fact",
            identity=identity,
            reference_time=observed_at,
            valid_at=fact.valid_at,
            invalid_at=fact.invalid_at,
            source_ids=fact.source_ids,
            confidence=fact.confidence,
            provenance_type=fact.provenance_type,
            evidence=fact.evidence,
            fields={
                "subject": fact.subject,
                "predicate": fact.predicate,
                "object": fact.object,
                "observed_at": _iso(observed_at),
            },
        )

    def _claim_episode(
        self,
        case_id: str,
        claim: Claim,
        availability: dict[str, datetime] | None = None,
    ) -> GraphEpisode:
        # ``stated_at`` stays the validity anchor; ``observed_at`` (recorded,
        # or the latest bound-material publication) is the knowledge boundary.
        # A claim stated early but recorded only by a later material stays out
        # of states that predate that material.
        observed_at = claim.observed_at or claim.stated_at
        floor = self._material_floor(availability, claim.based_on)
        if floor is not None and floor > observed_at:
            observed_at = floor
        return self._make_episode(
            case_id=case_id,
            kind="claim",
            identity=claim.claim_id,
            reference_time=observed_at,
            valid_at=claim.stated_at,
            invalid_at=None,
            source_ids=claim.based_on,
            evidence=claim.evidence,
            confidence=claim.confidence,
            provenance_type=claim.provenance_type,
            fields={
                "claim_id": claim.claim_id,
                "actor": claim.actor,
                "proposition": claim.proposition,
                "stance": claim.stance,
                "based_on": list(claim.based_on),
                "revised_by": claim.revised_by,
                "observed_at": _iso(observed_at),
                "claim_type": claim.claim_type,
            },
        )

    def _material_episode(self, case_id: str, material: Material) -> GraphEpisode:
        # Raw content, filesystem paths and URLs are deliberately excluded. The
        # corpus remains the readable source of truth and may contain secrets or
        # signed query parameters that do not belong in graph/LLM payloads.
        return self._make_episode(
            case_id=case_id,
            kind="material_provenance",
            identity=material.id,
            reference_time=material.published_at,
            valid_at=material.published_at,
            invalid_at=None,
            source_ids=(material.id,),
            fields={
                "material_id": material.id,
                "title": material.title,
                "source": material.source,
                "published_at": _iso(material.published_at),
                "fetched_at": _iso(material.fetched_at),
                "material_type": material.type,
                "original_format": material.original_format,
                "ocr": material.ocr,
                "extracted_via": material.extracted_via,
                "case_tags": list(material.case_tags),
            },
        )

    @staticmethod
    def _timeline_entry(
        episode: GraphEpisode, *, as_of: datetime | None = None
    ) -> TimelineEntry:
        payload = json.loads(episode.episode_body)
        if episode.kind == "evolution_case" and as_of is not None:
            status_at = payload.get("status_at")
            status_observed_at = payload.get("status_observed_at")
            try:
                status_visible = (
                    datetime.fromisoformat(status_at) <= as_of
                    and datetime.fromisoformat(status_observed_at) <= as_of
                )
            except (TypeError, ValueError):
                status_visible = True
            if not status_visible:
                payload["status"] = None
        summaries = {
            "evolution_case": payload.get("canonical_name"),
            "evolution_node": payload.get("summary"),
            "temporal_fact": " ".join(
                str(payload.get(field, ""))
                for field in ("subject", "predicate", "object")
            ).strip(),
            "claim": payload.get("proposition"),
            "material_provenance": payload.get("title"),
        }
        summary = summaries.get(episode.kind) or episode.name
        return TimelineEntry(
            episode_key=episode.episode_key,
            case_id=episode.case_id,
            kind=episode.kind,
            summary=summary,
            reference_time=episode.reference_time,
            valid_at=episode.valid_at,
            invalid_at=episode.invalid_at,
            source_ids=episode.source_ids,
            confidence=episode.confidence,
            provenance_type=episode.provenance_type,
            stance=payload.get("stance"),
            payload=_json(payload),
            evidence=episode.evidence,
        )
