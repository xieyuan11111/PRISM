"""Automatic accumulation and merged graph writes for evolution cases.

:class:`CaseService` closes the loop between per-material extraction and the
graph: evidence with a case is recorded under that case id, while substantive
case-less evidence is retained separately as ``awaiting_case_binding`` in the
durable :class:`~prism.cases.ledger.CaseExtractionLedger`. The
accumulated evidence of that case is merged through the conservative
:class:`~prism.cases.CaseBundleMerger`, and the merged bundle — never one
full case per material — is written to the injected graph service.

Conservative rules preserved from the merger: only explicitly recorded
materials enter a merge; foreign node case ids require an explicit
``adopt_case`` decision; unknown sources and identifier collisions raise
instead of silently overwriting evidence; conflicting alternatives are never
auto-resolved.  A failed merge or graph write rolls the just-recorded ledger
row back to the previous state, so the ledger only ever contains materials
whose merged case write succeeded.  Evidence gaps, unresolved conflicts and
extraction warnings are retained in the ledger verbatim and surfaced in every
:class:`CaseWriteOutcome` — they never silently disappear.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from prism.domain import EvolutionCase, Material
from prism.extraction import ExtractionEvidenceGap, ExtractionResult
from prism.graph import GraphWriteResult

from .ledger import (
    CaseExtractionLedger,
    CaseLedgerEntry,
    MaterialCaseConflict,
    MaterialEvidenceEntry,
)


class _GraphWriter(Protocol):
    async def add_case(
        self,
        case: EvolutionCase,
        *,
        nodes: Iterable = (),
        facts: Iterable = (),
        claims: Iterable = (),
        relations: Iterable = (),
        conflicts: Iterable = (),
        materials: Iterable = (),
    ) -> GraphWriteResult: ...


# The merged case's identity fields the first recorded case record fixes.
# ``node_ids`` is excluded on purpose: the merger always rebuilds it from the
# accumulated nodes, so it is derived data rather than case identity.
_CASE_IDENTITY_FIELDS = (
    "case_type",
    "canonical_name",
    "start_at",
    "status",
    "status_at",
    "status_observed_at",
)


@dataclass(frozen=True, slots=True)
class CaseWriteOutcome:
    """The auditable outcome of one accumulated case merge-and-write."""

    case_id: str
    bundle: object  # MergedCaseBundle (typed loosely to avoid a cycle)
    write: GraphWriteResult
    material_ids: tuple[str, ...]
    warnings: tuple[str, ...]


class CaseService:
    """Accumulate per-material extractions per case and write merged bundles.

    One material binds one case: :meth:`record_extraction` refuses (with an
    explicit :class:`MaterialCaseConflict`) any attempt to record a material
    that is already bound under a different case, so the automatic path can
    never add ambiguous bindings.  All merge-and-write operations are
    serialized through one asyncio lock so a failed merge's ledger rollback
    can never interleave with another merge of the same case.
    """

    def __init__(
        self,
        *,
        ledger: CaseExtractionLedger,
        merger: object,
        graph_service: _GraphWriter,
    ) -> None:
        if ledger is None:
            raise ValueError("ledger is required")
        if merger is None or not callable(getattr(merger, "merge", None)):
            raise TypeError("merger must provide merge()")
        if graph_service is None or not callable(
            getattr(graph_service, "add_case", None)
        ):
            raise TypeError("graph_service must provide add_case()")
        self._ledger = ledger
        self._merger = merger
        self._graph = graph_service
        self._lock = asyncio.Lock()

    async def record_extraction(
        self, material: Material, extraction: ExtractionResult
    ) -> CaseWriteOutcome:
        """Record one successful extraction and rewrite the merged case.

        The material must not already be bound under a different case: that
        re-binding is refused before any row is written.  The ledger row is
        written first and rolled back if the subsequent merge or graph write
        fails, so a material that poisons its case (a foreign node case
        without adoption, an unknown source, an identifier collision) never
        enters the accumulated state.
        """
        if not isinstance(material, Material):
            raise TypeError("material must be a Material")
        if not isinstance(extraction, ExtractionResult):
            raise TypeError("extraction must be an ExtractionResult")
        if extraction.case is None:
            raise ValueError(
                "extraction must carry a case to be recorded; caseless "
                "extractions are indexed but never accumulated"
            )
        case_id = extraction.case.case_id
        async with self._lock:
            return await self._record_bound_locked(material, extraction)

    async def record_material_extraction(
        self, material: Material, extraction: ExtractionResult
    ) -> MaterialEvidenceEntry:
        """Persist graph-ready candidates that have no case yet.

        This path writes only PRISM's local SQLite ledger.  It never calls
        the graph and never derives a case from title, tags, embeddings, or
        candidate identifiers.
        """
        if not isinstance(material, Material):
            raise TypeError("material must be a Material")
        if not isinstance(extraction, ExtractionResult):
            raise TypeError("extraction must be an ExtractionResult")
        if extraction.case is not None:
            raise ValueError("material-scoped extraction must not declare a case")
        if extraction.accumulation_status != "awaiting_case_binding":
            raise ValueError(
                "material-scoped accumulation requires substantive candidates"
            )
        if not any(
            gap.gap_type == "missing_case_context"
            for gap in extraction.evidence_gaps
        ):
            extraction = replace(
                extraction,
                evidence_gaps=extraction.evidence_gaps
                + (
                    ExtractionEvidenceGap(
                        "missing_case_context",
                        "validated candidates are retained at material scope "
                        "until an explicit case binding is supplied",
                        source_ids=(material.id,),
                    ),
                ),
            )
        self._validate_material_candidates(material, extraction)
        async with self._lock:
            if self._ledger.case_ids_for_material(material.id):
                raise MaterialCaseConflict(
                    material.id, self._ledger.case_ids_for_material(material.id)
                )
            return self._ledger.record_material(material, extraction)

    async def bind_material_to_case(
        self, material_id: str, case_id: str
    ) -> CaseWriteOutcome:
        """Explicitly bind one pending material to an already known case.

        The existing case record supplies all case metadata; this method does
        not synthesize a case from the material.  Evidence/source/time
        integrity is rechecked against the stored material immediately before
        the bound extraction enters the normal merge-and-graph path.
        """
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        async with self._lock:
            pending = self._ledger.material_entry(material_id)
            if pending is None:
                raise LookupError(
                    f"no material-scoped evidence awaiting binding for {material_id!r}"
                )
            entries = self._ledger.entries(case_id)
            if not entries:
                raise LookupError(
                    f"explicit binding requires an existing case {case_id!r}"
                )
            bound_cases = self._ledger.case_ids_for_material(material_id)
            if bound_cases and case_id not in bound_cases:
                raise MaterialCaseConflict(
                    material_id, bound_cases, attempted_case=case_id
                )
            self._validate_material_candidates(
                pending.material, pending.extraction
            )
            template = self._template_case(case_id, entries)
            bound = replace(
                pending.extraction,
                case=template,
                nodes=tuple(
                    replace(node, case_id=case_id)
                    for node in pending.extraction.nodes
                ),
                evidence_gaps=tuple(
                    gap
                    for gap in pending.extraction.evidence_gaps
                    if gap.gap_type != "missing_case_context"
                ),
                accumulation_status="case_bound",
            )
            outcome = await self._record_bound_locked(pending.material, bound)
            self._ledger.remove_material(material_id)
            return outcome

    async def merge_case(self, case_id: str) -> CaseWriteOutcome | None:
        """Rebuild and rewrite the accumulated case; ``None`` if unknown."""
        async with self._lock:
            return await self._merge_entries(
                case_id, self._ledger.entries(case_id)
            )

    async def merge_explicit(
        self,
        case_id: str,
        material_ids: Iterable[str],
    ) -> CaseWriteOutcome:
        """Merge an explicitly selected subset of a case's materials.

        Only materials already accumulated under ``case_id`` can be selected;
        an unknown id raises :class:`LookupError` instead of being skipped.
        Materials extracted under a foreign case id can never be selected —
        they fail :meth:`record_extraction` under the merger's conservative
        rules, and adopting them is an explicit human decision reserved for
        the later arbitration capability.
        """
        requested = tuple(material_ids)
        async with self._lock:
            entries = self._ledger.entries(case_id)
            by_id = {entry.material_id: entry for entry in entries}
            missing = tuple(
                material_id
                for material_id in requested
                if material_id not in by_id
            )
            if missing:
                raise LookupError(
                    "no recorded extraction for case "
                    f"{case_id!r} and material(s): {', '.join(missing)}"
                )
            return await self._merge_entries(
                case_id,
                tuple(by_id[material_id] for material_id in requested),
            )

    def case_ids_for_material(self, material_id: str) -> tuple[str, ...]:
        """Every case a material is bound to (legacy rows stay readable)."""
        return self._ledger.case_ids_for_material(material_id)

    def case_for_material(self, material_id: str) -> str | None:
        """The case a material was accumulated under, or ``None``.

        A legacy material bound under several cases raises the typed
        :class:`MaterialCaseConflict`; its rows remain readable through
        :meth:`case_ids_for_material`.
        """
        return self._ledger.case_for_material(material_id)

    def load_case(self, case_id: str) -> EvolutionCase:
        """Load the real recorded evolution case for ``case_id``.

        The case record is the identity of the first accumulated material's
        extraction (the same template a merge keeps); ``node_ids`` is derived
        data and is returned empty.  This loader never synthesizes a case
        from a title, tag, or embedding: an unknown ``case_id`` raises
        :class:`LookupError` so the caller can fail explicitly instead of
        guessing.
        """
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        entries = self._ledger.entries(case_id)
        if not entries:
            raise LookupError(
                f"no recorded evolution case {case_id!r}; record a material "
                "under the case first or declare the case explicitly"
            )
        return self._template_case(case_id, entries)

    # ------------------------------------------------------------- internals

    async def _record_bound_locked(
        self, material: Material, extraction: ExtractionResult
    ) -> CaseWriteOutcome:
        case = extraction.case
        if case is None:  # pragma: no cover - guarded by public entry point
            raise ValueError("bound extraction must declare a case")
        case_id = case.case_id
        bound = self._ledger.case_ids_for_material(material.id)
        if bound and case_id not in bound:
            raise MaterialCaseConflict(material.id, bound, attempted_case=case_id)
        previous = self._ledger.record(case_id, material, extraction)
        try:
            outcome = await self._merge_entries(
                case_id, self._ledger.entries(case_id)
            )
        except BaseException as error:
            try:
                self._rollback(case_id, material.id, previous)
            except Exception as rollback_error:
                raise RuntimeError(
                    f"ledger rollback for material {material.id!r} failed "
                    f"after merge error: {rollback_error}"
                ) from error
            raise
        if outcome is None:  # pragma: no cover - ledger row exists by construction
            raise RuntimeError("ledger lost the just-recorded entry")
        return outcome

    @staticmethod
    def _validate_material_candidates(
        material: Material, extraction: ExtractionResult
    ) -> None:
        """Recheck persisted candidates without trusting old ledger JSON."""
        collections = (
            ("node", extraction.nodes, "source_ids"),
            ("temporal_fact", extraction.temporal_facts, "source_ids"),
            ("claim", extraction.claims, "based_on"),
            ("conflict", extraction.conflicts, "source_ids"),
            ("relation", extraction.relations, "source_ids"),
        )
        candidates = [
            (kind, candidate, source_field)
            for kind, items, source_field in collections
            for candidate in items
        ]
        if not candidates:
            raise ValueError("material-scoped evidence has no candidates")
        substantive = False
        for kind, candidate, source_field in candidates:
            evidence_role = getattr(candidate, "evidence_role", None)
            if evidence_role == "context_only" or evidence_role is None:
                raise ValueError(
                    f"{kind} candidate requires a substantive evidence_role"
                )
            if evidence_role != "publication_event":
                substantive = True
            source_ids = tuple(getattr(candidate, source_field))
            if not source_ids or any(item != material.id for item in source_ids):
                raise ValueError(
                    f"{kind} candidate source ids must reference only stored "
                    f"material {material.id!r}"
                )
            evidence = tuple(getattr(candidate, "evidence", ()))
            if not evidence:
                raise ValueError(f"{kind} candidate requires source evidence")
            for locator in evidence:
                if locator.source_id != material.id:
                    raise ValueError(
                        f"{kind} evidence must reference stored material {material.id!r}"
                    )
                if locator.quote is None or locator.quote not in material.content:
                    raise ValueError(
                        f"{kind} evidence quote is not present verbatim in stored material"
                    )
            CaseService._validate_candidate_times(kind, candidate, material)
        if not substantive:
            raise ValueError(
                "publication-only material evidence cannot become substantive case evidence"
            )

    @staticmethod
    def _validate_candidate_times(
        kind: str, candidate: object, material: Material
    ) -> None:
        for name in (
            "happened_at",
            "valid_at",
            "invalid_at",
            "observed_at",
            "stated_at",
        ):
            value = getattr(candidate, name, None)
            if value is not None:
                if not isinstance(value, datetime):
                    raise TypeError(f"{kind}.{name} must be a datetime")
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError(f"{kind}.{name} must be timezone-aware")
                if value > material.published_at:
                    raise ValueError(
                        f"{kind}.{name} must not be later than material publication"
                    )
        observed = getattr(candidate, "observed_at", None)
        anchor = getattr(candidate, "happened_at", None)
        if anchor is None:
            anchor = getattr(candidate, "stated_at", None)
        if anchor is None:
            anchor = getattr(candidate, "valid_at", None)
        if observed is not None and anchor is not None and observed < anchor:
            raise ValueError(f"{kind}.observed_at must not precede its time anchor")

    def _rollback(
        self,
        case_id: str,
        material_id: str,
        previous: tuple[str, str] | None,
    ) -> None:
        if previous is None:
            self._ledger.remove(case_id, material_id)
        else:
            self._ledger.record_raw(
                case_id, material_id, previous[0], previous[1]
            )

    async def _merge_entries(
        self,
        case_id: str,
        entries: tuple[CaseLedgerEntry, ...],
    ) -> CaseWriteOutcome | None:
        from prism.cases import CaseEvidence  # package cycle safety

        if not entries:
            return None
        template = self._template_case(case_id, entries)
        warnings: list[str] = []
        self._warn_diverging_case_records(case_id, entries, template, warnings)

        evidence = [
            CaseEvidence(
                material=entry.material,
                extraction=entry.extraction,
                adopt_case=False,
            )
            for entry in entries
        ]
        bundle = self._merger.merge(template, evidence)
        warnings.extend(bundle.warnings)
        for entry in entries:
            self._collect_audit_warnings(entry, warnings)

        graph_arguments = {
            "nodes": bundle.nodes,
            "facts": bundle.temporal_facts,
            "claims": bundle.claims,
            "materials": bundle.materials,
        }
        add_case = self._graph.add_case
        if bundle.relations and self._accepts_kwarg(add_case, "relations"):
            graph_arguments["relations"] = bundle.relations
        if bundle.conflicts and self._accepts_kwarg(add_case, "conflicts"):
            graph_arguments["conflicts"] = bundle.conflicts
        write = await add_case(bundle.case, **graph_arguments)
        if not isinstance(write, GraphWriteResult):
            raise TypeError(
                "graph_service must return a GraphWriteResult, got "
                f"{type(write).__name__}"
            )
        return CaseWriteOutcome(
            case_id=case_id,
            bundle=bundle,
            write=write,
            material_ids=tuple(entry.material_id for entry in entries),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _template_case(
        case_id: str, entries: tuple[CaseLedgerEntry, ...]
    ) -> EvolutionCase:
        recorded = entries[0].extraction.case
        if recorded is None or recorded.case_id != case_id:
            raise ValueError(
                f"ledger entry for case {case_id!r} does not declare that case"
            )
        # node_ids are rebuilt by the merger from the accumulated nodes.
        return EvolutionCase(
            case_id=recorded.case_id,
            case_type=recorded.case_type,
            canonical_name=recorded.canonical_name,
            start_at=recorded.start_at,
            status=recorded.status,
            node_ids=(),
            status_at=recorded.status_at,
            status_observed_at=recorded.status_observed_at,
        )

    @staticmethod
    def _warn_diverging_case_records(
        case_id: str,
        entries: tuple[CaseLedgerEntry, ...],
        template: EvolutionCase,
        warnings: list[str],
    ) -> None:
        for entry in entries[1:]:
            reported = entry.extraction.case
            if reported is None:
                continue
            differing = tuple(
                name
                for name in _CASE_IDENTITY_FIELDS
                if getattr(reported, name) != getattr(template, name)
            )
            if differing:
                warnings.append(
                    f"material {entry.material_id!r} reports differing case "
                    f"metadata ({', '.join(differing)}) for case {case_id!r}; "
                    "the first recorded case record is kept"
                )

    @staticmethod
    def _collect_audit_warnings(
        entry: CaseLedgerEntry, warnings: list[str]
    ) -> None:
        extraction = entry.extraction
        material_id = entry.material_id
        if extraction.evidence_gaps:
            types = ", ".join(
                sorted({gap.gap_type for gap in extraction.evidence_gaps})
            )
            warnings.append(
                f"material {material_id!r} retains "
                f"{len(extraction.evidence_gaps)} evidence gap(s) ({types})"
            )
        if extraction.conflicts:
            ids = ", ".join(
                conflict.conflict_id for conflict in extraction.conflicts
            )
            warnings.append(
                f"material {material_id!r} retains "
                f"{len(extraction.conflicts)} unresolved conflict(s): {ids}"
            )
        for warning in extraction.warnings:
            warnings.append(
                f"material {material_id!r} extraction warning: {warning}"
            )

    @staticmethod
    def _accepts_kwarg(method: object, name: str) -> bool:
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or parameter.name == name
            for parameter in parameters
        )


__all__ = ["CaseService", "CaseWriteOutcome"]
