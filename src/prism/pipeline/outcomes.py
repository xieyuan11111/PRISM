"""Structured, queryable lifecycle outcomes for the automatic pipeline.

One material announced to the automatic pipeline has exactly one current
lifecycle outcome: ``pending`` while its run is in flight, ``failed`` when an
attempt raised, ``committed`` when a run completed successfully (every stage —
including the accumulated-case merge and graph write — finished).  The records
carry the audit fields required for a durable failure trail (``material_id``,
``stage``, ``error_type``, ``occurred_at``) plus a status so that querying
state never confuses "queued/in flight" with "done" or with success.

:class:`PipelineOutcomeLedger` persists the TERMINAL outcomes (``failed`` and
``committed``) in the SAME local SQLite file as the evidence store and the
case-extraction ledger (``index.db`` under ``PathConfig.data_dir``), one row
per material — the current outcome, never history.  This is deliberately a
local, single-process-file ledger, not a cross-process outbox: it survives a
process restart so operators can audit which materials failed, but it does not
ship events between processes.  ``pending`` is transient and lives only in the
running process (a crash mid-run leaves no stale pending row: the material is
simply uncommitted and safe to retry).

Only terminal, validated outcomes are written; a failed attempt never records
a fake success, and a later successful retry replaces the stale failure row.

The same ledger also persists the current rebuildable RUN AUDIT per material
(:class:`PipelineRunAudit`, table ``pipeline_stage_audits``): the projected
stage records of the latest attempt plus the report-version link the append
flow recorded.  It exists purely so restart-surviving status views can
replay recorded audit data (H-6: a read-only projection, never a new fact
store — no temporal semantics, no stage result payloads); re-running the
material rebuilds the row under the same key.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from prism.config import PathConfig
from prism.store.service import DB_FILENAME

#: Table holding the current lifecycle outcome of every processed material.
TABLE = "pipeline_outcomes"

#: Table holding the current rebuildable run audit of every processed
#: material: the projected stage records (name/status/detail only — never
#: stage result payloads) plus the run window and the report version the
#: append flow linked to this material.  One current row per material.
RUN_AUDIT_TABLE = "pipeline_stage_audits"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    material_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    stage TEXT,
    error_type TEXT,
    message TEXT,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT,
    updated_at TEXT NOT NULL
);
"""

_UPSERT_SQL = f"""
INSERT INTO {TABLE} (
    material_id, status, stage, error_type, message, occurred_at,
    correlation_id, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(material_id) DO UPDATE SET
    status = excluded.status,
    stage = excluded.stage,
    error_type = excluded.error_type,
    message = excluded.message,
    occurred_at = excluded.occurred_at,
    correlation_id = excluded.correlation_id,
    updated_at = excluded.updated_at
"""

_RUN_AUDIT_DDL = f"""
CREATE TABLE IF NOT EXISTS {RUN_AUDIT_TABLE} (
    material_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    stages TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    correlation_id TEXT,
    report_version_id TEXT,
    updated_at TEXT NOT NULL
);
"""

_RUN_AUDIT_UPSERT_SQL = f"""
INSERT INTO {RUN_AUDIT_TABLE} (
    material_id, status, stages, started_at, finished_at, correlation_id,
    report_version_id, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(material_id) DO UPDATE SET
    status = excluded.status,
    stages = excluded.stages,
    started_at = excluded.started_at,
    finished_at = excluded.finished_at,
    correlation_id = excluded.correlation_id,
    report_version_id = excluded.report_version_id,
    updated_at = excluded.updated_at
"""

_RUN_AUDIT_SELECT_SQL = (
    f"SELECT material_id, status, stages, started_at, finished_at, "
    f"correlation_id, report_version_id FROM {RUN_AUDIT_TABLE} "
    f"ORDER BY updated_at, material_id"
)

_SELECT_SQL = (
    f"SELECT material_id, status, stage, error_type, message, occurred_at, "
    f"correlation_id FROM {TABLE} ORDER BY occurred_at, material_id"
)

#: Lifecycle states of one material in the automatic pipeline.
PENDING = "pending"
FAILED = "failed"
COMMITTED = "committed"
OUTCOME_STATUSES = frozenset({PENDING, FAILED, COMMITTED})

#: Statuses of a recorded run audit: a completed attempt or a failed one.
AUDIT_COMPLETED = "completed"
AUDIT_FAILED = "failed"
RUN_AUDIT_STATUSES = frozenset({AUDIT_COMPLETED, AUDIT_FAILED})

#: Stage names allowed inside a run audit (mirrors PipelineStage).
_AUDIT_STAGE_NAMES = frozenset({"index", "extract", "graph"})


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _require_text(name, value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(name: str, value: str) -> datetime:
    parsed = datetime.fromisoformat(
        value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    """The current automatic-pipeline outcome of one material.

    ``status`` is ``pending`` (an attempt is in flight), ``failed`` (the last
    attempt raised; ``stage`` names the failing pipeline stage or is ``None``
    when the failure preceded any stage, and ``error_type``/``message``
    describe the underlying error) or ``committed`` (an attempt completed:
    index, extraction and — when a case was produced — the accumulated-case
    merge and graph write all succeeded).  ``occurred_at`` is when the state
    was entered: the run start for pending, the failure time for failed, the
    run finish for committed.
    """

    material_id: str
    status: str
    occurred_at: datetime
    stage: str | None = None
    error_type: str | None = None
    message: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_text("material_id", self.material_id)
        if self.status not in OUTCOME_STATUSES:
            raise ValueError(
                "status must be one of: " + ", ".join(sorted(OUTCOME_STATUSES))
            )
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("occurred_at must be timezone-aware")
        object.__setattr__(self, "stage", _optional_text("stage", self.stage))
        object.__setattr__(
            self, "error_type", _optional_text("error_type", self.error_type)
        )
        object.__setattr__(self, "message", _optional_text("message", self.message))
        object.__setattr__(
            self,
            "correlation_id",
            _optional_text("correlation_id", self.correlation_id),
        )
        if self.status == FAILED:
            if self.error_type is None:
                raise ValueError("failed outcomes require error_type")
            if self.message is None:
                raise ValueError("failed outcomes require message")
        elif (
            self.stage is not None
            or self.error_type is not None
            or self.message is not None
        ):
            raise ValueError(
                "stage, error_type and message describe failures only; "
                "pending and committed outcomes must not carry them"
            )


@dataclass(frozen=True, slots=True)
class StageAuditRecord:
    """The audit projection of one pipeline stage (never its result payload).

    ``name``/``status``/``detail`` mirror the in-process
    :class:`~prism.pipeline.service.PipelineStage` audit fields verbatim;
    the stage ``result`` objects (an index outcome carries the material's
    full document content) are deliberately not persisted or projected.
    """

    name: str
    status: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.name not in _AUDIT_STAGE_NAMES:
            raise ValueError(
                "name must be one of: " + ", ".join(sorted(_AUDIT_STAGE_NAMES))
            )
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("status must be a non-empty string")
        if self.detail is not None and (
            not isinstance(self.detail, str) or not self.detail.strip()
        ):
            raise ValueError("detail must be a non-empty string")

    def as_dict(self) -> dict[str, str | None]:
        """JSON-safe projection for persistence and status views."""
        return {"name": self.name, "status": self.status, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: object) -> "StageAuditRecord":
        if not isinstance(data, dict):
            raise ValueError("stage audit record must be a JSON object")
        unknown = set(data) - {"name", "status", "detail"}
        if unknown:
            raise ValueError(
                "unknown stage audit fields: " + ", ".join(sorted(unknown))
            )
        return cls(
            name=data["name"],
            status=data["status"],
            detail=data.get("detail"),
        )


@dataclass(frozen=True, slots=True)
class PipelineRunAudit:
    """The rebuildable audit of one material's current pipeline attempt.

    This is deliberately a re-derivable projection of the in-process
    :class:`~prism.pipeline.service.PipelineRun` (stage name/status/detail,
    run window, correlation id) plus the report-version link the append
    flow (:meth:`PrismAPI.add_material`) recorded after saving a version.
    It carries no new facts, no temporal semantics and no stage result
    payloads; re-running the material rebuilds it under the same key, and a
    successful retry replaces a stale failed audit exactly like the
    lifecycle outcome does.
    """

    material_id: str
    status: str
    stages: tuple[StageAuditRecord, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None
    correlation_id: str | None = None
    report_version_id: str | None = None

    def __post_init__(self) -> None:
        _require_text("material_id", self.material_id)
        if self.status not in RUN_AUDIT_STATUSES:
            raise ValueError(
                "status must be one of: " + ", ".join(sorted(RUN_AUDIT_STATUSES))
            )
        object.__setattr__(self, "stages", tuple(self.stages))
        for stage in self.stages:
            if not isinstance(stage, StageAuditRecord):
                raise TypeError("stages must contain only StageAuditRecord objects")
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError(f"{name} must be timezone-aware")
        object.__setattr__(
            self, "correlation_id", _optional_text("correlation_id", self.correlation_id)
        )
        object.__setattr__(
            self,
            "report_version_id",
            _optional_text("report_version_id", self.report_version_id),
        )


class PipelineOutcomeLedger:
    """SQLite-backed persistence for terminal pipeline outcomes.

    One ledger serves one PRISM environment (one data dir) and shares the
    EvidenceStore SQLite file, exactly like the case-extraction ledger; the
    table is created additively so older databases migrate in place.  Only
    terminal outcomes (``failed``/``committed``) are recorded — ``pending`` is
    an in-process transient state and is refused here — and each material
    keeps exactly one current row (upsert), so a successful retry replaces
    the stale failure instead of piling up history.  Importing this module
    touches no network and imports no optional extras.
    """

    def __init__(self, paths: PathConfig) -> None:
        if not isinstance(paths, PathConfig):
            raise TypeError("paths must be a PathConfig")
        database = paths.data_dir / DB_FILENAME
        database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database))
        self._connection.executescript(_DDL)
        self._connection.executescript(_RUN_AUDIT_DDL)
        self._connection.commit()
        self._closed = False

    def record(self, outcome: PipelineOutcome) -> None:
        """Upsert one terminal outcome as the material's current state."""
        if not isinstance(outcome, PipelineOutcome):
            raise TypeError("outcome must be a PipelineOutcome")
        if outcome.status == PENDING:
            raise ValueError(
                "pending is an in-process transient state and is never "
                "persisted; only failed and committed outcomes are durable"
            )
        now = _now_iso()
        self._connection.execute(
            _UPSERT_SQL,
            (
                outcome.material_id,
                outcome.status,
                outcome.stage,
                outcome.error_type,
                outcome.message,
                outcome.occurred_at.isoformat(),
                outcome.correlation_id,
                now,
            ),
        )
        self._connection.commit()

    def entries(self) -> tuple[PipelineOutcome, ...]:
        """Every recorded terminal outcome, in occurred-at order."""
        rows = self._connection.execute(_SELECT_SQL).fetchall()
        outcomes: list[PipelineOutcome] = []
        for (
            material_id,
            status,
            stage,
            error_type,
            message,
            occurred_at,
            correlation_id,
        ) in rows:
            outcomes.append(
                PipelineOutcome(
                    material_id=material_id,
                    status=status,
                    occurred_at=_parse_timestamp("occurred_at", occurred_at),
                    stage=stage,
                    error_type=error_type,
                    message=message,
                    correlation_id=correlation_id,
                )
            )
        return tuple(outcomes)

    def close(self) -> None:
        """Close the SQLite connection.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._connection.close()

    def record_run_audit(self, audit: PipelineRunAudit) -> None:
        """Upsert one material's current run audit (rebuildable projection).

        Like :meth:`record`, each material keeps exactly one current row: a
        successful retry replaces the stale failed audit instead of piling
        up history, and the append flow's report-version annotation rewrites
        the same row.  The stage records persist as JSON of
        ``StageAuditRecord.as_dict()`` values — name/status/detail only.
        """
        if not isinstance(audit, PipelineRunAudit):
            raise TypeError("audit must be a PipelineRunAudit")
        now = _now_iso()
        self._connection.execute(
            _RUN_AUDIT_UPSERT_SQL,
            (
                audit.material_id,
                audit.status,
                json.dumps(
                    [stage.as_dict() for stage in audit.stages],
                    ensure_ascii=False,
                ),
                audit.started_at.isoformat() if audit.started_at else None,
                audit.finished_at.isoformat() if audit.finished_at else None,
                audit.correlation_id,
                audit.report_version_id,
                now,
            ),
        )
        self._connection.commit()

    def record_terminal(
        self, outcome: PipelineOutcome, audit: PipelineRunAudit
    ) -> None:
        """Persist one terminal outcome AND its run audit in one transaction.

        Both rows are upserted inside a single SQLite transaction and
        committed together, so a crash or a mid-write failure can never
        leave the forbidden state — a durable committed outcome whose run
        audit is missing.  The pair must describe the same material and the
        outcome must be terminal (``pending`` is refused exactly like in
        :meth:`record`); a refused pair writes neither row.
        """
        if not isinstance(outcome, PipelineOutcome):
            raise TypeError("outcome must be a PipelineOutcome")
        if not isinstance(audit, PipelineRunAudit):
            raise TypeError("audit must be a PipelineRunAudit")
        if outcome.material_id != audit.material_id:
            raise ValueError(
                "outcome and audit must describe the same material: "
                f"{outcome.material_id!r} vs {audit.material_id!r}"
            )
        if outcome.status == PENDING:
            raise ValueError(
                "pending is an in-process transient state and is never "
                "persisted; only failed and committed outcomes are durable"
            )
        now = _now_iso()
        try:
            self._connection.execute(
                _UPSERT_SQL,
                (
                    outcome.material_id,
                    outcome.status,
                    outcome.stage,
                    outcome.error_type,
                    outcome.message,
                    outcome.occurred_at.isoformat(),
                    outcome.correlation_id,
                    now,
                ),
            )
            self._connection.execute(
                _RUN_AUDIT_UPSERT_SQL,
                (
                    audit.material_id,
                    audit.status,
                    json.dumps(
                        [stage.as_dict() for stage in audit.stages],
                        ensure_ascii=False,
                    ),
                    audit.started_at.isoformat() if audit.started_at else None,
                    audit.finished_at.isoformat() if audit.finished_at else None,
                    audit.correlation_id,
                    audit.report_version_id,
                    now,
                ),
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def run_audit_entries(self) -> tuple[PipelineRunAudit, ...]:
        """Every recorded run audit, in last-updated order."""
        rows = self._connection.execute(_RUN_AUDIT_SELECT_SQL).fetchall()
        audits: list[PipelineRunAudit] = []
        for (
            material_id,
            status,
            stages_json,
            started_at,
            finished_at,
            correlation_id,
            report_version_id,
        ) in rows:
            stages_data = json.loads(stages_json)
            if not isinstance(stages_data, list):
                raise ValueError("run audit stages must be a JSON array")
            audits.append(
                PipelineRunAudit(
                    material_id=material_id,
                    status=status,
                    stages=tuple(
                        StageAuditRecord.from_dict(item) for item in stages_data
                    ),
                    started_at=(
                        _parse_timestamp("started_at", started_at)
                        if started_at
                        else None
                    ),
                    finished_at=(
                        _parse_timestamp("finished_at", finished_at)
                        if finished_at
                        else None
                    ),
                    correlation_id=correlation_id,
                    report_version_id=report_version_id,
                )
            )
        return tuple(audits)


__all__ = [
    "AUDIT_COMPLETED",
    "AUDIT_FAILED",
    "COMMITTED",
    "FAILED",
    "OUTCOME_STATUSES",
    "PENDING",
    "PipelineOutcome",
    "PipelineOutcomeLedger",
    "PipelineRunAudit",
    "RUN_AUDIT_STATUSES",
    "RUN_AUDIT_TABLE",
    "StageAuditRecord",
]
