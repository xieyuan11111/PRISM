"""Experimental two-stage, evidence-bound extraction for Flash models.

split-v1 is not a production extraction path.  It exists for a controlled
prompt-profile experiment in which one large strict envelope is replaced by:

* Stage A: material role, case, nodes and temporal facts;
* Stage B: claims, conflicts and relations, restricted to Stage A's accepted
  canonical IDs.

Both stages are still parsed by :class:`ExtractionService`'s strict parser.
This module only projects the smaller stage envelopes onto that unchanged
parser contract and merges the validated results deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from prism.domain import EvolutionCase, Material

from prism.extraction.profiles import build_profiled_prompt, normalize_prompt_profile
from prism.extraction.service import (
    GAP_PAYLOAD_EVIDENCE_FIELDS,
    GAP_PAYLOAD_FIELDS,
    ExtractionEvidenceGap,
    ExtractionError,
    ExtractionResult,
    ExtractionService,
    _json_plain,
)


class _CompletionLike(Protocol):
    text: str


class _RouterLike(Protocol):
    async def complete(self, role: str, prompt: str) -> _CompletionLike: ...


class SplitExtractionError(ExtractionError):
    """A split-v1 stage failed closed, without carrying provider content."""

    def __init__(self, stage: str, message: str | None = None) -> None:
        if stage not in {"stage_a", "stage_b"}:
            raise ValueError("stage must be 'stage_a' or 'stage_b'")
        self.stage = stage
        super().__init__(
            message
            or f"split-v1 {stage} failed strict completion or envelope validation"
        )


_STAGE_A_FIELDS = frozenset(
    {"material_role", "case", "nodes", "temporal_facts", "warnings"}
)
_STAGE_B_FIELDS = frozenset({"claims", "conflicts", "relations", "warnings"})


def _strict_candidate_snapshot(kind: str, candidate: object) -> dict[str, Any]:
    """Reconstruct the model-facing JSON accepted for one Stage A candidate."""

    if not isinstance(candidate, object):
        raise TypeError("candidate must be an object")
    plain = _json_plain(candidate)
    if not isinstance(plain, dict):
        raise TypeError("candidate must serialize to a JSON object")
    allowed = GAP_PAYLOAD_FIELDS[kind]
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in plain:
            continue
        value = plain[key]
        if key == "evidence" and isinstance(value, list):
            result[key] = [
                {
                    item_key: item_value
                    for item_key, item_value in item.items()
                    if item_key in GAP_PAYLOAD_EVIDENCE_FIELDS
                }
                if isinstance(item, Mapping)
                else item
                for item in value
            ]
        else:
            result[key] = value
    if kind in ("node", "temporal_fact"):
        result.setdefault("assertion_type", "fact")
    return result


def _gap_delta(
    previous: tuple[ExtractionEvidenceGap, ...],
    combined: tuple[ExtractionEvidenceGap, ...],
) -> tuple[ExtractionEvidenceGap, ...]:
    """Gaps introduced by Stage B, after removing Stage A's exact gaps."""

    remaining = list(previous)
    added: list[ExtractionEvidenceGap] = []
    for gap in combined:
        try:
            remaining.remove(gap)
        except ValueError:
            added.append(gap)
    return tuple(added)


class SplitExtractionService:
    """Explicit experimental split-v1 extractor; never used by default composition."""

    def __init__(
        self,
        router: _RouterLike,
        *,
        evidence_locator: Any | None = None,
        prompt_profile: str | None = None,
    ) -> None:
        if router is None or not callable(getattr(router, "complete", None)):
            raise TypeError("router must provide an async complete method")
        self._prompt_profile = normalize_prompt_profile(prompt_profile)
        self._parser = ExtractionService(
            router,
            evidence_locator=evidence_locator,
            prompt_profile=prompt_profile,
        )

    async def extract(self, material: Material) -> ExtractionResult:
        """Legacy-compatible entry point, unchanged and never split."""

        return await self._parser.extract(material)

    async def extract_material(
        self,
        material: Material,
        *,
        corpus_path: str | Any | None = None,
        target_case: EvolutionCase | None = None,
    ) -> ExtractionResult:
        """Pipeline-compatible explicit use of the experimental split path."""

        return await self.extract_material_split(
            material, corpus_path=corpus_path, target_case=target_case
        )

    async def extract_material_split(
        self,
        material: Material,
        *,
        corpus_path: str | Any | None = None,
        target_case: EvolutionCase | None = None,
    ) -> ExtractionResult:
        if not isinstance(material, Material):
            raise TypeError("material must be a Material")
        if target_case is not None and not isinstance(target_case, EvolutionCase):
            raise TypeError("target_case must be an EvolutionCase or None")
        if self._parser._evidence_locator is None and corpus_path is None:
            raise ValueError(
                "corpus_path is required when no evidence_locator is configured"
            )

        stage_a = await self._run_stage_a(
            material, corpus_path=corpus_path, target_case=target_case
        )
        return await self._run_stage_b(
            material,
            stage_a,
            corpus_path=corpus_path,
            target_case=target_case,
        )

    async def _run_stage_a(
        self,
        material: Material,
        *,
        corpus_path: str | Any | None,
        target_case: EvolutionCase | None,
    ) -> ExtractionResult:
        prompt = self._profiled_prompt(
            self._stage_a_prompt(material, target_case), material.fetched_at
        )
        try:
            payload, syntax_warnings = await self._completion("stage_a", prompt)
            self._check_envelope("stage_a", payload, _STAGE_A_FIELDS)
            self._check_stage_a_claim_references(payload)
            projected = {
                "material_role": payload["material_role"],
                "case": payload["case"],
                "nodes": payload["nodes"],
                "temporal_facts": payload["temporal_facts"],
                "claims": [],
                "conflicts": [],
                "relations": [],
                "warnings": payload["warnings"],
            }
            return self._parser._parse_payload(
                projected,
                material,
                strict=True,
                corpus_path=corpus_path,
                syntax_warnings=syntax_warnings,
                target_case=target_case,
            )
        except SplitExtractionError:
            raise
        except Exception as error:
            raise SplitExtractionError("stage_a") from error

    async def _run_stage_b(
        self,
        material: Material,
        stage_a: ExtractionResult,
        *,
        corpus_path: str | Any | None,
        target_case: EvolutionCase | None,
    ) -> ExtractionResult:
        accepted_ids = {
            node.id for node in stage_a.nodes
        } | {
            fact.fact_id
            for fact in stage_a.temporal_facts
            if fact.fact_id is not None
        }
        prompt = self._profiled_prompt(
            self._stage_b_prompt(material, target_case, accepted_ids),
            material.fetched_at,
        )
        try:
            payload, syntax_warnings = await self._completion("stage_b", prompt)
            self._check_envelope("stage_b", payload, _STAGE_B_FIELDS)
            self._check_relation_references(payload, accepted_ids)
            projected = {
                "material_role": stage_a.material_role,
                "case": None if stage_a.case is None else _json_plain(stage_a.case),
                "nodes": [
                    _strict_candidate_snapshot("node", node)
                    for node in stage_a.nodes
                ],
                "temporal_facts": [
                    _strict_candidate_snapshot("temporal_fact", fact)
                    for fact in stage_a.temporal_facts
                ],
                "claims": payload["claims"],
                "conflicts": payload["conflicts"],
                "relations": payload["relations"],
                "warnings": [*stage_a.warnings, *payload["warnings"]],
            }
            combined = self._parser._parse_payload(
                projected,
                material,
                strict=True,
                corpus_path=corpus_path,
                syntax_warnings=syntax_warnings,
                target_case=target_case,
            )
        except Exception:
            return self._stage_b_failure(material, stage_a)

        return replace(
            combined,
            evidence_gaps=(
                stage_a.evidence_gaps
                + _gap_delta(stage_a.evidence_gaps, combined.evidence_gaps)
            ),
        )

    async def _completion(
        self, stage: str, prompt: str
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        completion = await self._parser._router.complete("extract", prompt)
        text = getattr(completion, "text", None)
        if not isinstance(text, str):
            raise SplitExtractionError(
                stage, f"split-v1 {stage} completion text must be a string"
            )
        try:
            return ExtractionService._load_payload_with_audit(text)
        except ExtractionError as error:
            raise SplitExtractionError(stage) from error

    @staticmethod
    def _check_envelope(
        stage: str, payload: dict[str, Any], fields: frozenset[str]
    ) -> None:
        if not isinstance(payload, dict):
            raise SplitExtractionError(stage)
        if set(payload) != fields:
            raise SplitExtractionError(
                stage,
                f"split-v1 {stage} completion must contain exactly these "
                f"fields: {', '.join(sorted(fields))}",
            )

    @staticmethod
    def _check_stage_a_claim_references(payload: dict[str, Any]) -> None:
        nodes = payload.get("nodes")
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if isinstance(node, dict) and node.get("claim_ids") not in (None, []):
                raise SplitExtractionError(
                    "stage_a",
                    "split-v1 stage A nodes must not reference claims; "
                    "claim_ids must be an empty array",
                )

    @staticmethod
    def _check_relation_references(
        payload: dict[str, Any], accepted_ids: set[str]
    ) -> None:
        relations = payload.get("relations")
        if not isinstance(relations, list):
            return
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            for field in ("source_ref", "target_ref"):
                if relation.get(field) not in accepted_ids:
                    raise SplitExtractionError(
                        "stage_b",
                        "split-v1 stage B relation references must use "
                        "Stage A accepted canonical IDs",
                    )

    @staticmethod
    def _stage_b_failure(
        material: Material, stage_a: ExtractionResult
    ) -> ExtractionResult:
        gap = ExtractionEvidenceGap(
            "stage_b_failure",
            "split-v1 stage B did not produce a validated completion; "
            "Stage A candidates were retained",
            source_ids=(material.id,),
        )
        warning = "split-v1 stage B failed; Stage A candidates were retained"
        return replace(
            stage_a,
            warnings=tuple(dict.fromkeys(stage_a.warnings + (warning,))),
            evidence_gaps=stage_a.evidence_gaps + (gap,),
        )

    def _profiled_prompt(self, prompt: str, fetched_at: Any) -> str:
        if self._prompt_profile is None:
            return prompt
        return build_profiled_prompt(
            self._prompt_profile, baseline_prompt=prompt, fetched_at=fetched_at
        )

    @staticmethod
    def _stage_a_prompt(
        material: Material, target_case: EvolutionCase | None
    ) -> str:
        baseline = ExtractionService._evolution_prompt(material, target_case)
        return (
            "SPLIT-V1 STAGE A EXPERIMENT. Return exactly one JSON object with "
            "exactly these top-level keys: material_role, case, nodes, "
            "temporal_facts, warnings. Every collection must be a JSON array, "
            "and warnings must be present even when empty. claims, conflicts, "
            "and relations are forbidden in this stage; do not return them, "
            "even as empty fields. nodes[].claim_ids must be an empty array "
            "because claims are produced only after Stage A is accepted. The "
            "complete target-case, source, verbatim-evidence, time, schema and "
            "candidate-boundary rules in the baseline prompt below still apply "
            "to every allowed field. Where that full-envelope prompt mentions "
            "claims, conflicts or relations, ignore those collections for this "
            "call.\n\n"
            + baseline
        )

    @staticmethod
    def _stage_b_prompt(
        material: Material,
        target_case: EvolutionCase | None,
        accepted_ids: set[str],
    ) -> str:
        baseline = ExtractionService._evolution_prompt(material, target_case)
        ids = ", ".join(sorted(accepted_ids)) if accepted_ids else "(none)"
        return (
            "SPLIT-V1 STAGE B EXPERIMENT. Stage A was already accepted. "
            "Return exactly one JSON object with exactly these top-level keys: "
            "claims, conflicts, relations, warnings. Every collection must be "
            "a JSON array, and warnings must be present even when empty. "
            "Do not return nodes or temporal_facts, and do not resend any "
            "Stage A candidate. claims, conflicts and relations may all be "
            "empty. Every relation source_ref and target_ref must be exactly "
            f"one of these Stage A accepted canonical IDs: {ids}. Never "
            "reference a Stage B claim, an invented ID, or an ID that Stage A "
            "proposed but did not have accepted. The complete target-case, "
            "source, verbatim-evidence, time, schema and candidate-boundary "
            "rules in the baseline prompt below still apply to every Stage B "
            "field. Where that full-envelope prompt describes Stage A nodes or "
            "temporal_facts, treat them as already accepted context and do not "
            "return them.\n\n"
            + baseline
        )
