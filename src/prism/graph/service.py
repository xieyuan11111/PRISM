"""Domain-to-episode mapping and offline historical timeline queries."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact

from .backend import GraphBackend
from .models import (
    GraphEpisode,
    GraphTimeline,
    GraphWriteResult,
    TimelineEntry,
    require_aware,
    require_text,
)


SCHEMA = "prism.graph.episode.v1"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        """Incrementally add a case and its source-backed evolution records."""
        episodes = [self._case_episode(case)]
        for node in nodes:
            if node.case_id != case.case_id:
                raise ValueError(
                    f"node {node.id!r} belongs to {node.case_id!r}, not {case.case_id!r}"
                )
            episodes.append(self._node_episode(node))
        episodes.extend(self._fact_episode(case.case_id, fact) for fact in facts)
        episodes.extend(self._claim_episode(case.case_id, claim) for claim in claims)
        episodes.extend(
            self._material_episode(case.case_id, material) for material in materials
        )

        added: list[str] = []
        skipped: list[str] = []
        for episode in episodes:
            destination = added if await self.backend.add_episode(episode) else skipped
            destination.append(episode.episode_key)
        return GraphWriteResult(tuple(episodes), tuple(added), tuple(skipped))

    async def timeline(self, case_id: str, as_of: datetime) -> GraphTimeline:
        """Return entries valid at ``as_of`` using ``[valid_at, invalid_at)``."""
        require_text("case_id", case_id)
        require_aware("as_of", as_of)
        results = await self.backend.search(f"PRISM timeline for case_id={case_id}")
        entries = [
            self._timeline_entry(episode)
            for episode in results
            if episode.case_id == case_id
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
    ) -> GraphEpisode:
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "case_id": case_id,
            "kind": kind,
            "reference_time": _iso(reference_time),
            "valid_at": _iso(valid_at),
            "invalid_at": _iso(invalid_at),
            "source_ids": list(source_ids),
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
                "node_ids": list(case.node_ids),
            },
        )

    def _node_episode(self, node: EvolutionNode) -> GraphEpisode:
        return self._make_episode(
            case_id=node.case_id,
            kind="evolution_node",
            identity=node.id,
            reference_time=node.happened_at,
            valid_at=node.happened_at,
            invalid_at=None,
            source_ids=node.source_ids,
            fields={
                "node_id": node.id,
                "node_type": node.node_type,
                "summary": node.summary,
                "claim_ids": list(node.claim_ids),
            },
        )

    def _fact_episode(self, case_id: str, fact: TemporalFact) -> GraphEpisode:
        identity = f"{fact.subject}:{fact.predicate}:{fact.object}"
        return self._make_episode(
            case_id=case_id,
            kind="temporal_fact",
            identity=identity,
            reference_time=fact.observed_at,
            valid_at=fact.valid_at,
            invalid_at=fact.invalid_at,
            source_ids=fact.source_ids,
            confidence=fact.confidence,
            provenance_type=fact.provenance_type,
            fields={
                "subject": fact.subject,
                "predicate": fact.predicate,
                "object": fact.object,
                "observed_at": _iso(fact.observed_at),
            },
        )

    def _claim_episode(self, case_id: str, claim: Claim) -> GraphEpisode:
        return self._make_episode(
            case_id=case_id,
            kind="claim",
            identity=claim.claim_id,
            reference_time=claim.stated_at,
            valid_at=claim.stated_at,
            invalid_at=None,
            source_ids=claim.based_on,
            fields={
                "claim_id": claim.claim_id,
                "actor": claim.actor,
                "proposition": claim.proposition,
                "stance": claim.stance,
                "based_on": list(claim.based_on),
                "revised_by": claim.revised_by,
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
    def _timeline_entry(episode: GraphEpisode) -> TimelineEntry:
        payload = json.loads(episode.episode_body)
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
            payload=episode.episode_body,
        )
