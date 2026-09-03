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
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from prism.config import PathConfig
from prism.store.service import DB_FILENAME

#: Table holding the current lifecycle outcome of every processed material.
TABLE = "pipeline_outcomes"

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

_SELECT_SQL = (
    f"SELECT material_id, status, stage, error_type, message, occurred_at, "
    f"correlation_id FROM {TABLE} ORDER BY occurred_at, material_id"
)

#: Lifecycle states of one material in the automatic pipeline.
PENDING = "pending"
FAILED = "failed"
COMMITTED = "committed"
OUTCOME_STATUSES = frozenset({PENDING, FAILED, COMMITTED})


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


__all__ = [
    "COMMITTED",
    "FAILED",
    "OUTCOME_STATUSES",
    "PENDING",
    "PipelineOutcome",
    "PipelineOutcomeLedger",
]
