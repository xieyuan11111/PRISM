"""Conservative loader for legacy merged-case JSON bundles.

Legacy merged-case files predate the M0 temporal contracts: their nodes often
carry ``happened_at`` but no ``observed_at``/``evidence``, their claims carry no
``observed_at``, and their facts can record an ``observed_at`` that is earlier
than the publication time of the materials that support them.  Naively mapping
such records into the graph made every entry visible from its *validity* time,
so materials published after a cutoff leaked (backfilled) into historical
states that predate them.

This loader is the formal, reusable ingestion boundary for those bundles.  It
never fabricates an observation time.  For every substantive record it:

1. resolves the bound materials (from the bundle's own ``materials`` section,
   then from an optional caller-supplied resolver such as an evidence store);
2. prefers a recorded ``observed_at`` when present, but never lets it precede
   the latest availability (publication) time of the bound materials — a fact,
   node or claim cannot be treated as observed before the material(s) that
   assert it were published;
3. when ``observed_at`` is absent and the bound materials resolve, derives it
   from their latest ``published_at`` (falling back to ``fetched_at`` only when
   a legacy material carries no publication time);
4. when no observation time can be determined, records an explicit
   ``observed_at_undetermined`` issue instead of inventing a time.  Facts (whose
   domain model requires ``observed_at``) are skipped with that issue; nodes
   and claims are kept with ``observed_at=None`` so the graph's recorded-anchor
   fallback stays visible and auditable.

Every conservative resolution or problem is returned as a structured
:class:`LegacyLoadIssue`; nothing is silently rewritten or dropped.

Accepted document shape (snake_case, ISO-8601 timezone-aware timestamps)::

    {
      "case": {case_id, case_type, canonical_name, start_at, status,
               node_ids?, status_at?, status_observed_at?},
      "nodes": [{id, case_id, node_type, happened_at, summary,
                 source_ids?, claim_ids?, valid_at?, observed_at?,
                 evidence?}],
      "temporal_facts": [{subject, predicate, object, valid_at,
                          confidence, provenance_type, invalid_at?,
                          observed_at?, source_ids?, evidence?}],
      "claims": [{claim_id, actor, proposition, stance, stated_at,
                  based_on? | source_ids?, revised_by?, observed_at?,
                  evidence?}],
      "materials": [{id, title, source, published_at, fetched_at, type,
                     content?, ...}],      // optional; may be omitted and
                                           // resolved externally instead
      "warnings": [string]
    }

``evidence`` items are ``{source_id?, corpus_path, paragraph?, page?,
quote?}``; ``source_id`` may be omitted only when the record binds exactly one
source.  Extra keys on any record are tolerated so older tooling output stays
loadable, but missing required keys, invalid timestamps and unverifiable
cross-references skip the affected record with an explicit issue.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prism.domain import Claim, EvidenceLocator, EvolutionCase, EvolutionNode, TemporalFact

if TYPE_CHECKING:
    from prism.cases import MergedCaseBundle

# Issue codes produced by the loader.
ISSUE_OBSERVED_AT_DERIVED = "observed_at_derived"
ISSUE_OBSERVED_AT_BOUNDED_LATER = "observed_at_bounded_later"
ISSUE_OBSERVED_AT_UNDETERMINED = "observed_at_undetermined"
ISSUE_RECORD_SKIPPED = "record_skipped"
ISSUE_RECORD_ID_COLLISION = "record_id_collision"
ISSUE_EVIDENCE_INVALID = "evidence_locator_invalid"
ISSUE_MATERIAL_TIME_UNRESOLVED = "material_time_unresolved"
ISSUE_STATUS_OBSERVED_AT_MISSING = "status_observed_at_missing"

ISSUE_CODES = frozenset(
    {
        ISSUE_OBSERVED_AT_DERIVED,
        ISSUE_OBSERVED_AT_BOUNDED_LATER,
        ISSUE_OBSERVED_AT_UNDETERMINED,
        ISSUE_RECORD_SKIPPED,
        ISSUE_RECORD_ID_COLLISION,
        ISSUE_EVIDENCE_INVALID,
        ISSUE_MATERIAL_TIME_UNRESOLVED,
        ISSUE_STATUS_OBSERVED_AT_MISSING,
    }
)

# A resolved material only needs to expose its availability times.
_Availability = Mapping[str, Any]  # published_at / fetched_at keys
_MaterialResolver = Callable[[str], _Availability | None]


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _timestamp(path: str, value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a timezone-aware ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
    except ValueError as error:
        raise ValueError(f"{path} must be a valid ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{path} must be timezone-aware")
    return parsed


def _optional_timestamp(path: str, value: object) -> datetime | None:
    if value is None:
        return None
    return _timestamp(path, value)


def _text_array(path: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a JSON array")
    return tuple(_require_text(f"{path}[{index}]", item) for index, item in enumerate(value))


def _record_dict(path: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a JSON object")
    return value


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class LegacyLoadIssue:
    """One explicit, structured outcome of a conservative legacy load.

    ``code`` is a stable machine-readable taxonomy entry; ``record_kind`` and
    ``record_id`` identify the affected record (case, node, temporal_fact,
    claim or material); ``detail`` states exactly what was derived, bounded,
    skipped or left undetermined, and why.
    """

    code: str
    record_kind: str
    record_id: str
    detail: str

    def __post_init__(self) -> None:
        _require_text("code", self.code)
        if self.code not in ISSUE_CODES:
            allowed = ", ".join(sorted(ISSUE_CODES))
            raise ValueError(f"code must be one of: {allowed}")
        _require_text("record_kind", self.record_kind)
        _require_text("record_id", self.record_id)
        _require_text("detail", self.detail)


@dataclass(frozen=True, slots=True)
class LegacyBundleLoadResult:
    """A loaded bundle plus every conservative decision made while loading.

    ``bundle`` contains only records that are representable and
    time-placeable under the conservative rules; every skipped or
    time-undetermined record is reported in ``issues`` so no data disappears
    silently.
    """

    bundle: "MergedCaseBundle"
    issues: tuple[LegacyLoadIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issues",
            tuple(
                sorted(
                    self.issues,
                    key=lambda issue: (
                        issue.code,
                        issue.record_kind,
                        issue.record_id,
                    ),
                )
            ),
        )


class LegacyCaseLoader:
    """Load legacy merged-case JSON under conservative temporal rules."""

    def __init__(
        self,
        material_resolver: _MaterialResolver | None = None,
    ) -> None:
        """``material_resolver`` maps a material id to an object with
        ``published_at``/``fetched_at`` (e.g. an ``IndexEntry`` or
        ``Material``) or to a mapping with those keys, returning ``None`` when
        the id is unknown."""
        if material_resolver is not None and not callable(material_resolver):
            raise TypeError("material_resolver must be callable")
        self._resolver = material_resolver

    def load(self, source: dict[str, Any] | str | Path) -> LegacyBundleLoadResult:
        """Load one legacy merged-case document.

        ``source`` may be a parsed dict, a JSON string, or a path to a JSON
        file.  Document-level failures (not a JSON object, no parseable case)
        raise ``ValueError``; record-level problems become issues.
        """
        payload = self._document(source)
        issues: list[LegacyLoadIssue] = []
        warnings: list[str] = []

        raw_materials, availability = self._load_materials(
            payload.get("materials"), issues
        )
        case = self._load_case(payload.get("case"), issues)

        nodes, claims = self._load_nodes_and_claims(payload, availability, issues)
        facts = self._load_facts(payload, availability, issues)

        raw_warnings = payload.get("warnings")
        if raw_warnings is not None:
            if not isinstance(raw_warnings, list):
                raise ValueError("warnings must be a JSON array")
            warnings.extend(
                _require_text(f"warnings[{index}]", item)
                for index, item in enumerate(raw_warnings)
            )

        # Imported lazily: this module is imported from prism.cases.
        from prism.cases import MergedCaseBundle

        bundle = MergedCaseBundle(
            case=case,
            nodes=tuple(nodes),
            temporal_facts=tuple(facts),
            claims=tuple(claims),
            materials=tuple(raw_materials),
            warnings=tuple(dict.fromkeys(warnings)),
        )
        return LegacyBundleLoadResult(bundle, tuple(issues))

    # ------------------------------------------------------------------ doc

    def _document(self, source: dict[str, Any] | str | Path) -> dict[str, Any]:
        if isinstance(source, dict):
            payload = source
        elif isinstance(source, Path):
            try:
                text = source.read_text(encoding="utf-8")
            except OSError as error:
                raise ValueError(f"cannot read legacy bundle {source}: {error}") from error
            return self._document(text)
        elif isinstance(source, str):
            try:
                parsed = json.loads(source)
            except json.JSONDecodeError as error:
                raise ValueError(f"legacy bundle is not valid JSON: {error}") from error
            if not isinstance(parsed, dict):
                raise ValueError("legacy bundle must contain a JSON object")
            payload = parsed
        else:
            raise TypeError(
                "source must be a dict, a JSON string, or a Path to a JSON file"
            )
        for collection in ("nodes", "temporal_facts", "claims", "materials"):
            value = payload.get(collection)
            if value is not None and not isinstance(value, list):
                raise ValueError(f"{collection} must be a JSON array")
        return payload

    # -------------------------------------------------------------- materials

    def _load_materials(
        self,
        raw: object,
        issues: list[LegacyLoadIssue],
    ) -> tuple[list[Any], dict[str, datetime]]:
        """Return (constructible Materials, id -> availability time).

        Availability uses the legacy record's ``published_at`` when present
        and falls back to ``fetched_at`` only for records that carry no
        publication time.  A record that carries neither contributes nothing;
        a record that cannot form a domain ``Material`` (missing required
        metadata or content) still contributes its availability time so entry
        observation times can be derived from it.
        """
        from prism.domain import Material

        materials: list[Material] = []
        availability: dict[str, datetime] = {}

        def note(code: str, record_id: str, detail: str) -> None:
            issues.append(LegacyLoadIssue(code, "material", record_id, detail))

        for index, item in enumerate(raw or ()):
            path = f"materials[{index}]"
            try:
                record = _record_dict(path, item)
                record_id = _require_text(f"{path}.id", record.get("id"))
                published = _optional_timestamp(
                    f"{path}.published_at", record.get("published_at")
                )
                fetched = _optional_timestamp(
                    f"{path}.fetched_at", record.get("fetched_at")
                )
            except ValueError as error:
                note(ISSUE_RECORD_SKIPPED, f"#{index}", str(error))
                continue
            if published is None and fetched is None:
                note(
                    ISSUE_MATERIAL_TIME_UNRESOLVED,
                    record_id,
                    "material carries neither published_at nor fetched_at; "
                    "it cannot bound observation times",
                )
                continue
            if published is not None:
                availability[record_id] = published
            else:
                availability[record_id] = fetched  # type: ignore[assignment]
                note(
                    ISSUE_MATERIAL_TIME_UNRESOLVED,
                    record_id,
                    "material has no published_at; using fetched_at as its "
                    "availability time",
                )
            try:
                materials.append(
                    Material(
                        id=record_id,
                        title=_require_text(f"{path}.title", record.get("title")),
                        source=_require_text(f"{path}.source", record.get("source")),
                        published_at=published,  # type: ignore[arg-type]
                        fetched_at=fetched,  # type: ignore[arg-type]
                        type=_require_text(f"{path}.type", record.get("type")),
                        content=_require_text(f"{path}.content", record.get("content")),
                        original_format=record.get("original_format"),
                        ocr=bool(record.get("ocr", False)),
                        extracted_via=record.get("extracted_via"),
                        raw_path=record.get("raw_path"),
                        case_tags=tuple(record.get("case_tags", ()) or ()),
                        url=record.get("url"),
                        retrieval_level=record.get("retrieval_level"),
                        access_level=record.get("access_level"),
                        doi=record.get("doi"),
                        authors=tuple(record.get("authors", ()) or ()),
                        container_title=record.get("container_title"),
                        pmid=record.get("pmid"),
                        pmcid=record.get("pmcid"),
                    )
                )
            except (TypeError, ValueError) as error:
                note(
                    ISSUE_RECORD_SKIPPED,
                    record_id,
                    f"material is not representable: {error}",
                )
        return materials, availability

    # ------------------------------------------------------------------ case

    def _load_case(
        self, value: object, issues: list[LegacyLoadIssue]
    ) -> EvolutionCase:
        if not isinstance(value, dict):
            raise ValueError("legacy bundle is missing a case object")
        case_id = _require_text("case.case_id", value.get("case_id"))
        try:
            status_at = _optional_timestamp("case.status_at", value.get("status_at"))
            status_observed_at = _optional_timestamp(
                "case.status_observed_at", value.get("status_observed_at")
            )
            case = EvolutionCase(
                case_id=case_id,
                case_type=_require_text("case.case_type", value.get("case_type")),
                canonical_name=_require_text(
                    "case.canonical_name", value.get("canonical_name")
                ),
                start_at=_timestamp("case.start_at", value.get("start_at")),
                status=_require_text("case.status", value.get("status")),
                node_ids=_text_array("case.node_ids", value.get("node_ids", [])),
                status_at=status_at,
                status_observed_at=status_observed_at,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid case record: {error}") from error
        if status_at is not None and status_observed_at is None:
            issues.append(
                LegacyLoadIssue(
                    ISSUE_STATUS_OBSERVED_AT_MISSING,
                    "case",
                    case_id,
                    "case records status_at but no status_observed_at; status "
                    "visibility is anchored to status_at and may predate the "
                    "material that reported it (late-source risk)",
                )
            )
        return case

    # ------------------------------------------------------------- records

    def _load_nodes_and_claims(
        self,
        payload: dict[str, Any],
        availability: dict[str, datetime],
        issues: list[LegacyLoadIssue],
    ) -> tuple[list[EvolutionNode], list[Claim]]:
        nodes: list[EvolutionNode] = []
        node_seen: dict[str, EvolutionNode] = {}
        claims: list[Claim] = []
        claim_seen: dict[str, Claim] = {}
        for index, item in enumerate(payload.get("nodes") or ()):
            self._load_node(index, item, availability, issues, nodes, node_seen)
        for index, item in enumerate(payload.get("claims") or ()):
            self._load_claim(index, item, availability, issues, claims, claim_seen)
        return nodes, claims

    def _load_node(
        self,
        index: int,
        value: object,
        availability: dict[str, datetime],
        issues: list[LegacyLoadIssue],
        nodes: list[EvolutionNode],
        seen: dict[str, EvolutionNode],
    ) -> None:
        path = f"nodes[{index}]"
        try:
            record = _record_dict(path, value)
            record_id = _require_text(f"{path}.id", record.get("id"))
            happened_at = _timestamp(f"{path}.happened_at", record.get("happened_at"))
            source_ids = _text_array(f"{path}.source_ids", record.get("source_ids", []))
            observed_value = _optional_timestamp(
                f"{path}.observed_at", record.get("observed_at")
            )
            valid_at = _optional_timestamp(f"{path}.valid_at", record.get("valid_at"))
            evidence = self._evidence_locators(
                path, record.get("evidence"), source_ids, issues, "node", record_id
            )
            observed = self._resolve_observed(
                record_kind="node",
                record_id=record_id,
                recorded=observed_value,
                bindings=source_ids,
                availability=availability,
                validity_anchor=happened_at,
                issues=issues,
            )
            node = EvolutionNode(
                id=record_id,
                case_id=_require_text(f"{path}.case_id", record.get("case_id")),
                node_type=_require_text(f"{path}.node_type", record.get("node_type")),
                happened_at=happened_at,
                summary=_require_text(f"{path}.summary", record.get("summary")),
                source_ids=source_ids,
                claim_ids=_text_array(f"{path}.claim_ids", record.get("claim_ids", [])),
                valid_at=valid_at,
                observed_at=observed,
                evidence=evidence,
                change_reason=record.get("change_reason"),
                provenance_type=record.get("provenance_type"),
            )
        except (TypeError, ValueError) as error:
            issues.append(
                LegacyLoadIssue(ISSUE_RECORD_SKIPPED, "node", f"#{index}", str(error))
            )
            return
        existing = seen.get(record_id)
        if existing is None:
            seen[record_id] = node
            nodes.append(node)
        elif existing != node:
            issues.append(
                LegacyLoadIssue(
                    ISSUE_RECORD_ID_COLLISION,
                    "node",
                    record_id,
                    "a second node record with this id differs from the first "
                    "and was skipped",
                )
            )

    def _load_claim(
        self,
        index: int,
        value: object,
        availability: dict[str, datetime],
        issues: list[LegacyLoadIssue],
        claims: list[Claim],
        seen: dict[str, Claim],
    ) -> None:
        path = f"claims[{index}]"
        try:
            record = _record_dict(path, value)
            record_id = _require_text(f"{path}.claim_id", record.get("claim_id"))
            based_on_value = record.get("based_on")
            if based_on_value is None:
                # Legacy claim payloads sometimes use the uniform source_ids
                # key; source_ids remains the canonical name for claims.
                based_on_value = record.get("source_ids", [])
            based_on = _text_array(f"{path}.based_on", based_on_value)
            observed_value = _optional_timestamp(
                f"{path}.observed_at", record.get("observed_at")
            )
            evidence = self._evidence_locators(
                path, record.get("evidence"), based_on, issues, "claim", record_id
            )
            observed = self._resolve_observed(
                record_kind="claim",
                record_id=record_id,
                recorded=observed_value,
                bindings=based_on,
                availability=availability,
                validity_anchor=None,
                issues=issues,
            )
            claim = Claim(
                claim_id=record_id,
                actor=_require_text(f"{path}.actor", record.get("actor")),
                proposition=_require_text(
                    f"{path}.proposition", record.get("proposition")
                ),
                stance=_require_text(f"{path}.stance", record.get("stance")),
                stated_at=_timestamp(f"{path}.stated_at", record.get("stated_at")),
                based_on=based_on,
                revised_by=record.get("revised_by"),
                evidence=evidence,
                observed_at=observed,
            )
        except (TypeError, ValueError) as error:
            issues.append(
                LegacyLoadIssue(ISSUE_RECORD_SKIPPED, "claim", f"#{index}", str(error))
            )
            return
        existing = seen.get(record_id)
        if existing is None:
            seen[record_id] = claim
            claims.append(claim)
        elif existing != claim:
            issues.append(
                LegacyLoadIssue(
                    ISSUE_RECORD_ID_COLLISION,
                    "claim",
                    record_id,
                    "a second claim record with this id differs from the first "
                    "and was skipped",
                )
            )

    def _load_facts(
        self,
        payload: dict[str, Any],
        availability: dict[str, datetime],
        issues: list[LegacyLoadIssue],
    ) -> list[TemporalFact]:
        facts: list[TemporalFact] = []
        seen: set[TemporalFact] = set()
        for index, item in enumerate(payload.get("temporal_facts") or ()):
            self._load_fact(index, item, availability, issues, facts, seen)
        return facts

    def _load_fact(
        self,
        index: int,
        value: object,
        availability: dict[str, datetime],
        issues: list[LegacyLoadIssue],
        facts: list[TemporalFact],
        seen: set[TemporalFact],
    ) -> None:
        path = f"temporal_facts[{index}]"
        try:
            record = _record_dict(path, value)
            subject = _require_text(f"{path}.subject", record.get("subject"))
            predicate = _require_text(f"{path}.predicate", record.get("predicate"))
            object_value = _require_text(f"{path}.object", record.get("object"))
            record_id = f"{subject}:{predicate}:{object_value}"
            valid_at = _timestamp(f"{path}.valid_at", record.get("valid_at"))
            invalid_at = _optional_timestamp(
                f"{path}.invalid_at", record.get("invalid_at")
            )
            source_ids = _text_array(f"{path}.source_ids", record.get("source_ids", []))
            observed_value = _optional_timestamp(
                f"{path}.observed_at", record.get("observed_at")
            )
            observed = self._resolve_observed(
                record_kind="temporal_fact",
                record_id=record_id,
                recorded=observed_value,
                bindings=source_ids,
                availability=availability,
                validity_anchor=None,
                issues=issues,
                report_undetermined=False,
            )
            if observed is None:
                # TemporalFact requires an observed_at; refusing to fabricate
                # one means this record cannot be represented at all.
                issues.append(
                    LegacyLoadIssue(
                        ISSUE_OBSERVED_AT_UNDETERMINED,
                        "temporal_fact",
                        record_id,
                        "fact has no recorded observed_at and none of its "
                        "source ids resolve to a material; the record cannot "
                        "be time-placed without fabricating an observation "
                        "time and is skipped",
                    )
                )
                return
            evidence = self._evidence_locators(
                path, record.get("evidence"), source_ids, issues, "temporal_fact", record_id
            )
            fact = TemporalFact(
                subject=subject,
                predicate=predicate,
                object=object_value,
                valid_at=valid_at,
                invalid_at=invalid_at,
                observed_at=observed,
                source_ids=source_ids,
                confidence=record.get("confidence"),
                provenance_type=_require_text(
                    f"{path}.provenance_type", record.get("provenance_type")
                ),
                evidence=evidence,
                fact_id=record.get("fact_id"),
            )
        except (TypeError, ValueError) as error:
            issues.append(
                LegacyLoadIssue(ISSUE_RECORD_SKIPPED, "temporal_fact", f"#{index}", str(error))
            )
            return
        if fact not in seen:
            seen.add(fact)
            facts.append(fact)
        # Exact duplicates collapse; differing duplicates are not possible
        # here because the fact identity above is value-derived.

    # ------------------------------------------------------------ core rules

    def _resolve_observed(
        self,
        *,
        record_kind: str,
        record_id: str,
        recorded: datetime | None,
        bindings: tuple[str, ...],
        availability: dict[str, datetime],
        validity_anchor: datetime | None,
        issues: list[LegacyLoadIssue],
        report_undetermined: bool = True,
    ) -> datetime | None:
        """Determine the conservative observation time for one record.

        Order of preference:

        * recorded ``observed_at`` — bounded below by the latest availability
          of the record's bound materials (a record cannot be treated as
          observed before the material(s) asserting it were published);
        * when absent and materials resolve — their latest availability,
          never earlier than the record's own validity anchor where one
          exists (a node cannot be observed before it happens);
        * otherwise ``None`` — never a fabricated time.

        Never returns a time earlier than the bound materials allow, and never
        guesses an observation time when nothing is knowable.  ``report_undetermined``
        lets callers that handle the undetermined case themselves (facts are
        skipped) avoid a duplicate issue.
        """
        available_time = self._bound_availability(bindings, availability)
        if recorded is not None:
            if (
                available_time is not None
                and available_time > recorded
            ):
                issues.append(
                    LegacyLoadIssue(
                        ISSUE_OBSERVED_AT_BOUNDED_LATER,
                        record_kind,
                        record_id,
                        "recorded observed_at "
                        f"{_iso(recorded)} is earlier than the latest bound "
                        f"material availability {_iso(available_time)}; "
                        "observation time is bounded by the material",
                    )
                )
                return available_time
            return recorded
        if available_time is not None:
            anchor = validity_anchor
            observed = (
                max(available_time, anchor)
                if anchor is not None
                else available_time
            )
            anchored = (
                " and the record's own happened_at anchor"
                if anchor is not None and anchor > available_time
                else ""
            )
            issues.append(
                LegacyLoadIssue(
                    ISSUE_OBSERVED_AT_DERIVED,
                    record_kind,
                    record_id,
                    "observed_at is absent; bounded to "
                    f"{_iso(observed)} by the latest bound material "
                    f"availability{anchored} — late sources cannot leak into "
                    "earlier states",
                )
            )
            return observed
        if report_undetermined:
            issues.append(
                LegacyLoadIssue(
                    ISSUE_OBSERVED_AT_UNDETERMINED,
                    record_kind,
                    record_id,
                    "no recorded observed_at and no bound material resolves; "
                    "observation time cannot be determined and is not fabricated "
                    "(the graph will fall back to the record's own time anchor, "
                    "which carries a late-source risk)",
                )
            )
        return None

    def _bound_availability(
        self,
        bindings: tuple[str, ...],
        availability: dict[str, datetime],
    ) -> datetime | None:
        """Latest availability time among the record's bound materials.

        Multi-source records use the latest necessary observation time —
        never the earliest — so a record that cites a late-published material
        cannot appear in states that predate it.
        """
        resolved = [
            availability[source_id]
            for source_id in bindings
            if source_id in availability
        ]
        if resolved:
            return max(resolved)
        if not bindings or self._resolver is None:
            return None
        resolved = []
        for source_id in bindings:
            found = self._resolver(source_id)
            if found is None:
                continue
            availability[source_id] = self._material_available_at(source_id, found)
            resolved.append(availability[source_id])
        return max(resolved) if resolved else None

    def _material_available_at(self, source_id: str, found: _Availability) -> datetime:
        """Extract the availability time from a resolver result.

        Accepts domain ``Material``/``IndexEntry`` objects (``published_at``
        attribute) or mappings with ``published_at``/``fetched_at`` keys.
        """
        published = getattr(found, "published_at", None)
        if published is None and isinstance(found, Mapping):
            published = found.get("published_at")
        if published is not None:
            return published
        fetched = getattr(found, "fetched_at", None)
        if fetched is None and isinstance(found, Mapping):
            fetched = found.get("fetched_at")
        if fetched is None:
            raise ValueError(
                f"material resolver returned no published_at/fetched_at for {source_id!r}"
            )
        return fetched

    # ------------------------------------------------------------- evidence

    def _evidence_locators(
        self,
        path: str,
        value: object,
        bindings: tuple[str, ...],
        issues: list[LegacyLoadIssue],
        record_kind: str,
        record_id: str,
    ) -> tuple[EvidenceLocator, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            issues.append(
                LegacyLoadIssue(
                    ISSUE_EVIDENCE_INVALID,
                    record_kind,
                    record_id,
                    f"{path}.evidence must be a JSON array; evidence ignored",
                )
            )
            return ()
        locators: list[EvidenceLocator] = []
        for index, item in enumerate(value):
            item_path = f"{path}.evidence[{index}]"
            try:
                item = _record_dict(item_path, item)
                source_id = item.get("source_id")
                if source_id is None:
                    if len(bindings) == 1:
                        source_id = bindings[0]
                    else:
                        raise ValueError(
                            "evidence source_id is required when a record binds "
                            "multiple sources"
                        )
                if source_id not in bindings:
                    raise ValueError(
                        f"evidence source_id {source_id!r} is not among the "
                        "record's bound sources"
                    )
                locators.append(
                    EvidenceLocator(
                        source_id=source_id,
                        corpus_path=_require_text(
                            f"{item_path}.corpus_path", item.get("corpus_path")
                        ),
                        paragraph=item.get("paragraph"),
                        page=item.get("page"),
                        quote=item.get("quote"),
                    )
                )
            except (TypeError, ValueError) as error:
                issues.append(
                    LegacyLoadIssue(
                        ISSUE_EVIDENCE_INVALID,
                        record_kind,
                        record_id,
                        f"{item_path}: {error}; locator ignored",
                    )
                )
        return tuple(locators)


__all__ = [
    "ISSUE_OBSERVED_AT_BOUNDED_LATER",
    "ISSUE_OBSERVED_AT_DERIVED",
    "ISSUE_OBSERVED_AT_UNDETERMINED",
    "ISSUE_CODES",
    "LegacyBundleLoadResult",
    "LegacyCaseLoader",
    "LegacyLoadIssue",
]
