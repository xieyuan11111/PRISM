"""Material journey projection seam for the PRISM workbench (WB-2/WB-3).

This module maps the facade's read-only
:class:`~prism.api.facade.MaterialJourneyView` onto the fixed seven-step
user journey — staged → ingested → indexed → extracted → merged →
graph_written → analyzed — using ONLY recorded audit data (the index entry,
the pipeline run's stage records, the durable run audit, the case binding
and the audit's report-version link).  It invents no state machine and no
facts (H-6): a step is ``completed``/``skipped`` only when a recorded audit
explicitly says so, a failure marks exactly the stage the ledger names,
everything else stays ``unknown``, and step times are recorded timestamps
only — the one per-step time that exists is the ledger's ``failed_at`` on
the failed step; no raw/corpus normalization-completion timestamp is
recorded anywhere (``fetched_at`` is the source's crawl time), so the other
steps show ``None`` instead of borrowing a nearby timestamp.  Pending,
partial, unknown and failure are never rendered as success (H-4).  The
retry entry point delegates to ``PrismAPI.process_material`` with the
material id — and only for a material whose CURRENT lifecycle is
``failed`` — reusing the pipeline's own idempotent retry semantics.

Everything here is dependency-free and NiceGUI-free; tests inject fake
facades returning real view objects.
"""

from __future__ import annotations

from typing import Any, Protocol

from prism.api.facade import MaterialJourneyView

from .materials import outcome_view, pipeline_view
from .status import lifecycle_ui_status

#: The fixed journey step order (WB-2.2 / requirements §7.2).
JOURNEY_STEPS = (
    "staged",
    "ingested",
    "indexed",
    "extracted",
    "merged",
    "graph_written",
    "analyzed",
)

_STEP_LABELS = {
    "staged": "Upload (staged)",
    "ingested": "Raw retention + Markdown normalization",
    "indexed": "Evidence index",
    "extracted": "Structured extraction",
    "merged": "Case accumulation merge",
    "graph_written": "Graph write",
    "analyzed": "Analysis / report version",
}

#: Journey step statuses: a recorded completion, a recorded skip with its
#: reason, the ledger's failed stage, or unknown (never a fabricated
#: success).
STEP_COMPLETED = "completed"
STEP_SKIPPED = "skipped"
STEP_FAILED = "failed"
STEP_UNKNOWN = "unknown"

#: Pipeline stage name -> journey step for the run-derived steps.
_STAGE_STEP_BY_NAME = {
    "index": "indexed",
    "extract": "extracted",
    "graph": "graph_written",
}

#: Failed-outcome stage name -> the journey step that stopped.
_FAILED_STAGE_STEP = {
    "index": "indexed",
    "extract": "extracted",
    "graph": "graph_written",
}

#: The recorded stage statuses that explicitly mean a stage completed: the
#: index stage records the store's ``IndexOutcome`` status ("indexed",
#: "updated", "unchanged"), extraction records "extracted" and the graph
#: stage records "written".  A recorded stage row carrying anything else —
#: a foreign or partially written status — projects as unknown, never as
#: completed (H-4).
_COMPLETED_STAGE_STATUSES = frozenset(
    {"indexed", "updated", "unchanged", "extracted", "written"}
)

#: Audit-level statuses safe to display verbatim; every other value (a
#: failed audit, a foreign value, ``None``) projects as unknown and never
#: as completed — the failure itself is carried by the failure triple.
_DISPLAYED_AUDIT_STATUSES = frozenset({STEP_COMPLETED, STEP_SKIPPED})


def _iso(value: object) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else None


def _stage_records(view: MaterialJourneyView) -> dict[str, dict[str, Any]]:
    """The recorded stage audit rows, preferring the live run's records."""
    records: dict[str, dict[str, Any]] = {}
    audit = view.run_audit
    for stage in tuple(getattr(audit, "stages", ()) or ()):
        records[getattr(stage, "name", "")] = {
            "status": getattr(stage, "status", None),
            "detail": getattr(stage, "detail", None),
        }
    run = view.run
    for stage in tuple(getattr(run, "stages", ()) or ()):
        records[getattr(stage, "name", "")] = {
            "status": getattr(stage, "status", None),
            "detail": getattr(stage, "detail", None),
        }
    return records


def journey_steps(view: MaterialJourneyView) -> list[dict[str, Any]]:
    """Project one journey view onto the fixed seven steps.

    Each step carries ``step``/``label``/``status``/``detail``/``time``.
    Statuses come only from recorded audit data — a stage row counts as
    completed only when its recorded status is an explicit completion
    status, and everything unrecognized stays ``unknown`` — the failed step
    is marked with the ledger's error type, skipped steps carry their
    recorded reason, and steps without evidence stay ``unknown``.  Step
    times are likewise only ever recorded facts: the ledger's
    ``failed_at`` for the failed step and nothing else, because no
    raw/corpus normalization-completion timestamp is recorded anywhere
    (``fetched_at`` is the source's crawl time, and the run window stays
    on the audit view rather than being re-stamped onto stages).
    """
    stages = _stage_records(view)
    failure = view.failure
    failed_stage = getattr(failure, "stage", None) if failure else None
    failed_step = _FAILED_STAGE_STEP.get(str(failed_stage or ""))
    steps: list[dict[str, Any]] = [
        {
            "step": "staged",
            "label": _STEP_LABELS["staged"],
            # The staging spool is a transient WebUI fact and leaves no
            # durable audit: the step stays unknown rather than borrowing
            # the index entry's fetched_at as fabricated proof (H-6).
            "status": STEP_UNKNOWN,
            "detail": (
                "staging is a transient WebUI spool and no durable audit "
                "records it for this material; the raw/corpus copies below "
                "are the authoritative retention"
            ),
            "time": None,
        },
        {
            "step": "ingested",
            "label": _STEP_LABELS["ingested"],
            # Raw retention plus corpus normalization are proven only by
            # the recorded pair of paths; one without the other is not.
            "status": (
                STEP_COMPLETED
                if view.raw_path and view.corpus_path
                else STEP_UNKNOWN
            ),
            "detail": (
                f"raw: {view.raw_path or 'not recorded'}; "
                f"corpus: {view.corpus_path or 'not recorded'}"
            ),
            # No normalization-completion timestamp is recorded anywhere;
            # fetched_at is the source's crawl time, not this step's time.
            "time": None,
        },
    ]
    for stage_name, step_name in (
        ("index", "indexed"),
        ("extract", "extracted"),
        ("graph", "graph_written"),
    ):
        record = stages.get(stage_name)
        if record is None:
            status, detail = STEP_UNKNOWN, "no recorded audit for this stage"
        elif record["status"] == "skipped":
            status = STEP_SKIPPED
            detail = record["detail"] or "skipped"
        elif record["status"] in _COMPLETED_STAGE_STATUSES:
            status, detail = STEP_COMPLETED, record["detail"]
        else:
            status = STEP_UNKNOWN
            detail = (
                f"recorded stage status {record['status']!r} is not a "
                "recognized completion"
            )
        steps.append({
            "step": step_name,
            "label": _STEP_LABELS[step_name],
            "status": status,
            "detail": detail,
            "time": None,
        })
    # The merged step: the accumulated-case binding is the recorded fact;
    # a skipped graph stage (with its reason) explains the absence.
    graph_record = stages.get("graph")
    if view.case_id:
        merged_status, merged_detail = (
            STEP_COMPLETED, f"case {view.case_id}"
        )
    elif graph_record is not None and graph_record["status"] == "skipped":
        merged_status = STEP_SKIPPED
        merged_detail = graph_record["detail"] or "no accumulated case"
    else:
        merged_status, merged_detail = STEP_UNKNOWN, "no recorded case binding"
    # Insert merged before graph_written per the fixed step order.
    steps.insert(-1, {
        "step": "merged",
        "label": _STEP_LABELS["merged"],
        "status": merged_status,
        "detail": merged_detail,
        "time": None,
    })
    steps.append({
        "step": "analyzed",
        "label": _STEP_LABELS["analyzed"],
        "status": (
            STEP_COMPLETED if view.report_version_id else STEP_UNKNOWN
        ),
        "detail": (
            f"report version {view.report_version_id}"
            if view.report_version_id
            else "no report version linked to this material's append"
        ),
        "time": None,
    })
    if failed_step is not None:
        error_type = getattr(failure, "error_type", None)
        for step in steps:
            if step["step"] == failed_step:
                step["status"] = STEP_FAILED
                step["detail"] = (
                    f"failed: {error_type}"
                    if error_type
                    else "failed at this stage"
                )
                # The one per-step timestamp that IS recorded: the
                # ledger's failure time.
                step["time"] = _iso(getattr(failure, "failed_at", None))
                break
    return steps


def _audit_view(view: MaterialJourneyView) -> dict[str, Any] | None:
    audit = view.run_audit
    if audit is None:
        return None
    status = getattr(audit, "status", None)
    return {
        # Only an explicitly recorded completed/skipped audit status is
        # displayed; anything else (a failed audit, a foreign value)
        # projects as unknown — never as completed (H-4).
        "status": (
            status if status in _DISPLAYED_AUDIT_STATUSES else STEP_UNKNOWN
        ),
        "stages": [
            {
                "name": getattr(stage, "name", None),
                "status": getattr(stage, "status", None),
                "detail": getattr(stage, "detail", None),
            }
            for stage in tuple(getattr(audit, "stages", ()) or ())
        ],
        "started_at": _iso(getattr(audit, "started_at", None)),
        "finished_at": _iso(getattr(audit, "finished_at", None)),
        "correlation_id": getattr(audit, "correlation_id", None),
        "report_version_id": getattr(audit, "report_version_id", None),
    }


def _failure_view(view: MaterialJourneyView) -> dict[str, Any] | None:
    failure = view.failure
    if failure is None:
        return None
    return {
        "stage": getattr(failure, "stage", None),
        "error_type": getattr(failure, "error_type", None),
        "message": getattr(failure, "message", None),
        "failed_at": _iso(getattr(failure, "failed_at", None)),
    }


def journey_view_data(view: MaterialJourneyView) -> dict[str, Any]:
    """Project one journey view into JSON-safe page data."""
    run = view.run
    return {
        "material_id": view.material_id,
        "display_name": view.display_name,
        "source_format": view.source_format,
        "case_id": view.case_id,
        "raw_path": view.raw_path,
        "corpus_path": view.corpus_path,
        "content_hash": view.content_hash,
        "fetched_at": _iso(view.fetched_at),
        "occurred_at": _iso(view.occurred_at),
        "lifecycle_status": view.lifecycle_status,
        "ui_status": lifecycle_ui_status(view.lifecycle_status),
        "run": pipeline_view(run) if run is not None else None,
        "audit": _audit_view(view),
        "failure": _failure_view(view),
        "mechanism_status": view.mechanism_status,
        "semantic_status": view.semantic_status,
        "evidence_gap_count": view.evidence_gap_count,
        "evidence_gaps": list(view.evidence_gaps),
        "unresolved_conflicts": list(view.unresolved_conflicts),
        "report_version_id": view.report_version_id,
        "steps": journey_steps(view),
    }


def material_row(view: MaterialJourneyView) -> dict[str, Any]:
    """One JSON-safe row for the material list table."""
    failure = view.failure
    return {
        "material_id": view.material_id,
        "display_name": view.display_name or view.material_id,
        "case_id": view.case_id,
        "lifecycle_status": view.lifecycle_status,
        "ui_status": lifecycle_ui_status(view.lifecycle_status),
        "mechanism_status": view.mechanism_status,
        "semantic_status": view.semantic_status,
        "evidence_gap_count": view.evidence_gap_count,
        "occurred_at": _iso(view.occurred_at),
        "failed_stage": getattr(failure, "stage", None) if failure else None,
        "error_type": getattr(failure, "error_type", None) if failure else None,
    }


_STEP_BADGES = {
    STEP_COMPLETED: "[completed]",
    STEP_SKIPPED: "[skipped]",
    STEP_FAILED: "[failed]",
    STEP_UNKNOWN: "[unknown]",
}


def journey_markdown(data: dict[str, Any]) -> str:
    """Render one journey projection as compact, auditable Markdown.

    Shows the honest lifecycle and quality layers (mechanism and semantic
    separately), the seven step badges with their audit details, the
    project-relative raw/corpus paths, the failure triple and the evidence
    gaps.  A ``partial`` semantic verdict renders as partial — never as a
    success color or word.
    """
    lines = [
        f"### Material `{data['material_id']}` — "
        f"{data.get('display_name') or '(no title recorded)'}",
        f"- Lifecycle: **{data['lifecycle_status']}**",
        f"- Mechanism: **{data['mechanism_status']}**",
        f"- Semantic: **{data['semantic_status']}**",
        f"- Evidence gaps: **{data['evidence_gap_count']}**",
        f"- Case: {data.get('case_id') or '_none recorded_'}",
        f"- Raw copy: `{data.get('raw_path') or 'not recorded'}`",
        f"- Corpus copy: `{data.get('corpus_path') or 'not recorded'}`",
    ]
    if data.get("report_version_id"):
        lines.append(f"- Report version: `{data['report_version_id']}`")
    failure = data.get("failure")
    if failure:
        failed_in = failure.get("stage") or "before any stage"
        lines.append(
            f"- Failure: stage **{failed_in}**, type "
            f"`{failure.get('error_type')}`, message: "
            f"{failure.get('message')}"
        )
    lines.append("#### Journey steps")
    for step in data["steps"]:
        badge = _STEP_BADGES.get(step["status"], "[unknown]")
        detail = f" — {step['detail']}" if step["detail"] else ""
        when = f" ({step['time']})" if step["time"] else ""
        lines.append(f"- {badge} **{step['step']}**{when}{detail}")
    gaps = data.get("evidence_gaps") or ()
    conflicts = data.get("unresolved_conflicts") or ()
    if gaps or conflicts:
        lines.append("#### Quality details")
        lines.extend(f"- Evidence gap: {gap}" for gap in gaps)
        lines.extend(f"- Conflict: {conflict}" for conflict in conflicts)
    return "\n".join(lines)


class JourneyFacade(Protocol):
    """The facade operations used by the journey views (WB-2.6)."""

    async def material_journey(self, material_id: str) -> MaterialJourneyView: ...

    async def material_journeys(
        self, *, case_id: str | None = None, status: str | None = None
    ) -> tuple[MaterialJourneyView, ...]: ...

    async def process_material(
        self,
        source: object,
        metadata: dict[str, Any] | None = None,
        *,
        target_case: object | None = None,
    ) -> object: ...


class MaterialJourneyController:
    """View-model adapter over the facade's read-only journey queries.

    All operations are async because the facade is async; all results are
    JSON-safe view data.  :meth:`retry` is the one explicit action: for a
    material whose current lifecycle is ``failed`` — and only then — it
    re-processes the material by id through ``PrismAPI.process_material``
    (the pipeline's own idempotent retry semantics — a success clears the
    stale failure, a persistent failure raises the structured
    :class:`~prism.pipeline.PipelineError` and is never rewritten into a
    fake success).
    """

    def __init__(self, api: JourneyFacade) -> None:
        for name in (
            "material_journey",
            "material_journeys",
            "process_material",
        ):
            if not callable(getattr(api, name, None)):
                raise TypeError(f"api must provide {name}()")
        self._api = api

    async def load_journeys(
        self, *, case_id: str | None = None, status: str | None = None
    ) -> dict[str, Any]:
        """Load the material list rows with optional case/status filters."""
        if case_id is not None and (
            not isinstance(case_id, str) or not case_id.strip()
        ):
            raise ValueError("case_id must be a non-empty string or None")
        if status is not None and status not in {
            "pending", "failed", "committed", "unknown",
        }:
            raise ValueError(
                "status must be one of: committed, failed, pending, unknown"
            )
        views = await self._api.material_journeys(
            case_id=case_id, status=status
        )
        rows = [material_row(view) for view in tuple(views)]
        return {"materials": rows, "count": len(rows)}

    async def load_journey(self, material_id: str) -> dict[str, Any]:
        """Load the full journey projection of one material."""
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        view = await self._api.material_journey(material_id.strip())
        return journey_view_data(view)

    async def retry(self, material_id: str) -> dict[str, Any]:
        """Retry one failed material by id through the facade.

        The gate is the material's CURRENT lifecycle, re-read from the
        journey query immediately before the write: only a ``failed``
        material is re-processed.  A committed material is refused —
        re-processing could rewrite its recorded evidence and break the
        append's report-version link — a pending material is already in
        flight, and an unknown lifecycle (or an unknown material id,
        which raises :class:`LookupError` in the query) has no auditable
        failure to retry.  Every refusal happens before any facade write.
        """
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        material_id = material_id.strip()
        view = await self._api.material_journey(material_id)
        if view.lifecycle_status != "failed":
            raise ValueError(
                f"material {material_id!r} is in the "
                f"{view.lifecycle_status!r} lifecycle; only failed "
                "materials can be retried"
            )
        result = await self._api.process_material(material_id)
        return outcome_view(result)

    @staticmethod
    def is_terminal(view: dict[str, Any]) -> bool:
        """Whether polling may stop: only committed/failed are terminal."""
        return view.get("lifecycle_status") in {"committed", "failed"}


__all__ = [
    "JOURNEY_STEPS",
    "JourneyFacade",
    "MaterialJourneyController",
    "journey_markdown",
    "journey_steps",
    "journey_view_data",
    "material_row",
]
