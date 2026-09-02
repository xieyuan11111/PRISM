"""Explicit, source-preserving assembly of one case from many materials."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.extraction import ExtractionResult


@dataclass(frozen=True, slots=True)
class CaseEvidence:
    """One material and its already validated extraction.

    ``adopt_case`` is an explicit caller decision allowing nodes extracted with
    a different local case id to be remapped to the target case.  It is never
    inferred from a title, tag, or semantic similarity.
    """

    material: Material
    extraction: ExtractionResult
    adopt_case: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.material, Material):
            raise TypeError("material must be a Material")
        if not isinstance(self.extraction, ExtractionResult):
            raise TypeError("extraction must be an ExtractionResult")
        if not isinstance(self.adopt_case, bool):
            raise TypeError("adopt_case must be a bool")


@dataclass(frozen=True, slots=True)
class MergedCaseBundle:
    """Graph-ready, deduplicated records for one explicitly chosen case.

    The bundle retains full in-memory ``Material`` objects for caller-side
    work, but it is not an audit serialization format.  Persist merged data
    through :class:`~prism.graph.GraphService`, whose payload allowlist omits
    material bodies, URLs, and filesystem paths.
    """

    case: EvolutionCase
    nodes: tuple[EvolutionNode, ...] = ()
    temporal_facts: tuple[TemporalFact, ...] = ()
    claims: tuple[Claim, ...] = ()
    materials: tuple[Material, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.case, EvolutionCase):
            raise TypeError("case must be an EvolutionCase")
        for name, expected in (
            ("nodes", EvolutionNode),
            ("temporal_facts", TemporalFact),
            ("claims", Claim),
            ("materials", Material),
        ):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, expected) for value in values):
                raise TypeError(f"{name} contains an invalid object")
            object.__setattr__(self, name, values)
        warnings = tuple(self.warnings)
        if any(not isinstance(value, str) or not value.strip() for value in warnings):
            raise ValueError("warnings must contain non-empty strings")
        object.__setattr__(self, "warnings", warnings)

    @property
    def source_ids(self) -> tuple[str, ...]:
        """Material ids in first-seen order, suitable for audit display."""
        return tuple(material.id for material in self.materials)


class CaseBundleMerger:
    """Merge explicitly selected material extractions into one case.

    This service is intentionally deterministic and conservative.  It does not
    decide whether a source belongs to a case.  Facts are deduplicated only
    when their complete immutable values match; observations with the same
    subject/predicate/object but different dates, confidence, sources, or
    validity are retained as distinct evidence.  Callers provide
    :class:`CaseEvidence`; foreign extraction case ids require
    ``adopt_case=True``.  Exact duplicates are collapsed, while identifier
    collisions with different payloads raise instead of silently overwriting
    evidence.
    """

    def merge(
        self,
        case: EvolutionCase,
        evidence: Iterable[CaseEvidence],
    ) -> MergedCaseBundle:
        if not isinstance(case, EvolutionCase):
            raise TypeError("case must be an EvolutionCase")

        evidence_items = tuple(evidence)
        for item in evidence_items:
            if not isinstance(item, CaseEvidence):
                raise TypeError("evidence must contain only CaseEvidence objects")

        materials: list[Material] = []
        material_by_id: dict[str, Material] = {}
        for item in evidence_items:
            material = item.material
            existing_material = material_by_id.get(material.id)
            if existing_material is not None and existing_material != material:
                raise ValueError(f"material id collision for {material.id!r}")
            if existing_material is None:
                material_by_id[material.id] = material
                materials.append(material)

        claim_scopes: dict[str, set[str]] = {}
        for item in evidence_items:
            for claim in item.extraction.claims:
                claim_scopes.setdefault(claim.claim_id, set()).add(
                    self._scoped_id(item.material.id, claim.claim_id)
                )

        nodes: list[EvolutionNode] = []
        node_by_id: dict[str, EvolutionNode] = {}
        facts: list[TemporalFact] = []
        fact_seen: set[TemporalFact] = set()
        claims: list[Claim] = []
        claim_by_id: dict[str, Claim] = {}
        warnings: list[str] = []

        for item in evidence_items:
            material = item.material
            extraction_case = item.extraction.case
            if extraction_case is not None and extraction_case.case_id != case.case_id:
                if not item.adopt_case:
                    raise ValueError(
                        f"material {material.id!r} has foreign case "
                        f"{extraction_case.case_id!r}; set adopt_case=True explicitly"
                    )
                warnings.append(
                    f"adopted extraction case {extraction_case.case_id!r} from "
                    f"material {material.id!r} into {case.case_id!r}"
                )

            claim_id_map = {
                claim.claim_id: self._scoped_id(material.id, claim.claim_id)
                for claim in item.extraction.claims
            }
            for node in item.extraction.nodes:
                if node.case_id != case.case_id:
                    if not item.adopt_case:
                        raise ValueError(
                            f"node {node.id!r} has foreign case {node.case_id!r}; "
                            "set adopt_case=True explicitly"
                        )
                    warnings.append(
                        f"adopted node case {node.case_id!r} from material "
                        f"{material.id!r} into {case.case_id!r}"
                    )
                if extraction_case is not None and node.case_id != extraction_case.case_id:
                    raise ValueError(
                        f"node {node.id!r} does not match its extraction case "
                        f"{extraction_case.case_id!r}"
                    )
                if any(source_id not in material_by_id for source_id in node.source_ids):
                    raise ValueError(
                        f"node {node.id!r} references an unknown source"
                    )
                if material.id not in node.source_ids:
                    raise ValueError(
                        f"node {node.id!r} from material {material.id!r} has no "
                        "matching source id"
                    )
                normalized = EvolutionNode(
                    id=self._scoped_id(material.id, node.id),
                    case_id=case.case_id,
                    node_type=node.node_type,
                    happened_at=node.happened_at,
                    summary=node.summary,
                    source_ids=node.source_ids,
                    claim_ids=tuple(
                        self._resolve_claim_id(
                            claim_id, material.id, claim_id_map, claim_scopes
                        )
                        for claim_id in node.claim_ids
                    ),
                )
                self._append_by_id(
                    normalized.id, normalized, node_by_id, nodes, "node"
                )

            for fact in item.extraction.temporal_facts:
                if any(source_id not in material_by_id for source_id in fact.source_ids):
                    raise ValueError("fact references an unknown source")
                if material.id not in fact.source_ids:
                    raise ValueError(
                        f"fact {fact.subject!r}/{fact.predicate!r} from material "
                        f"{material.id!r} has no matching source id"
                    )
                if fact not in fact_seen:
                    fact_seen.add(fact)
                    facts.append(fact)

            for claim in item.extraction.claims:
                if any(source_id not in material_by_id for source_id in claim.based_on):
                    raise ValueError(
                        f"claim {claim.claim_id!r} references an unknown source"
                    )
                if material.id not in claim.based_on:
                    raise ValueError(
                        f"claim {claim.claim_id!r} from material {material.id!r} "
                        "has no matching based_on source id"
                    )
                normalized_claim = Claim(
                    claim_id=claim_id_map[claim.claim_id],
                    actor=claim.actor,
                    proposition=claim.proposition,
                    stance=claim.stance,
                    stated_at=claim.stated_at,
                    based_on=claim.based_on,
                    revised_by=(
                        self._resolve_claim_id(
                            claim.revised_by,
                            material.id,
                            claim_id_map,
                            claim_scopes,
                        )
                        if claim.revised_by is not None
                        else None
                    ),
                )
                self._append_by_id(
                    normalized_claim.claim_id,
                    normalized_claim,
                    claim_by_id,
                    claims,
                    "claim",
                )

        node_ids: list[str] = []
        for node in nodes:
            if node.id not in node_ids:
                node_ids.append(node.id)
        omitted_case_nodes = tuple(node_id for node_id in case.node_ids if node_id not in node_ids)
        if omitted_case_nodes:
            warnings.append(
                "input case.node_ids were rebuilt from merged nodes; omitted "
                + ", ".join(omitted_case_nodes)
            )
        merged_case = EvolutionCase(
            case_id=case.case_id,
            case_type=case.case_type,
            canonical_name=case.canonical_name,
            start_at=case.start_at,
            status=case.status,
            node_ids=tuple(node_ids),
        )
        return MergedCaseBundle(
            case=merged_case,
            nodes=tuple(nodes),
            temporal_facts=tuple(facts),
            claims=tuple(claims),
            materials=tuple(materials),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _resolve_claim_id(
        local_id: str,
        material_id: str,
        local_claims: dict[str, str],
        all_claims: dict[str, set[str]],
    ) -> str:
        if local_id in local_claims:
            return local_claims[local_id]
        matches = all_claims.get(local_id, set())
        if len(matches) == 1:
            return next(iter(matches))
        if not matches:
            raise ValueError(f"claim reference {local_id!r} is unknown")
        raise ValueError(f"claim reference {local_id!r} is ambiguous across materials")

    @staticmethod
    def _scoped_id(material_id: str, local_id: str) -> str:
        return f"{material_id}::{local_id}"

    @staticmethod
    def _append_by_id(
        key: str,
        value: object,
        registry: dict[str, object],
        ordered: list[object],
        label: str,
    ) -> None:
        existing = registry.get(key)
        if existing is None:
            registry[key] = value
            ordered.append(value)
            return
        if existing != value:
            raise ValueError(f"{label} id collision for {key!r}")


__all__ = ["CaseBundleMerger", "CaseEvidence", "MergedCaseBundle"]
