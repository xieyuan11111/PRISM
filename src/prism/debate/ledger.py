"""Minimal SQLite audit ledger for automatic debate runs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from prism.config import PathConfig

from .models import (
    DebateFailure,
    DebateResult,
    DebateStatement,
    FollowUpResult,
    IndependentInterpretation,
    result_from_dict,
    result_to_dict,
)

TABLE = "debate_audit"
FOLLOW_UP_TABLE = "debate_followup_audit"


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class DebateAuditEntry:
    run_id: str
    input_hash: str
    case_id: str
    question: str
    as_of: datetime
    profiles: tuple[str, ...]
    status: str
    fallback_reason: str | None
    evidence_bundle_hash: str
    completed_at: datetime | None
    rounds_json: str
    result_json: str


def _follow_up_to_dict(result: FollowUpResult) -> dict[str, Any]:
    return {
        "follow_up_id": result.follow_up_id,
        "parent_run_id": result.parent_run_id,
        "case_id": result.case_id,
        "question": result.question,
        "as_of": result.as_of.isoformat(),
        "perspective_id": result.perspective_id,
        "evidence_bundle_hash": result.evidence_bundle_hash,
        "interpretation": None if result.interpretation is None else {
            "profile_id": result.interpretation.profile_id,
            "statements": [
                {"id": item.id, "classification": item.classification,
                 "text": item.text, "evidence_ids": list(item.evidence_ids)}
                for item in result.interpretation.statements
            ],
        },
        "status": result.status,
        "errors": [
            {"profile_id": item.profile_id, "phase": item.phase,
             "error_code": item.error_code, "message": item.message}
            for item in result.errors
        ],
        "warnings": list(result.warnings),
        "completed_at": None if result.completed_at is None else result.completed_at.isoformat(),
    }


def _follow_up_from_dict(data: dict[str, Any]) -> FollowUpResult:
    interpretation_data = data["interpretation"]
    interpretation = None
    if interpretation_data is not None:
        interpretation = IndependentInterpretation(
            profile_id=interpretation_data["profile_id"],
            statements=tuple(DebateStatement(**item) for item in interpretation_data["statements"]),
        )
    return FollowUpResult(
        follow_up_id=data["follow_up_id"], parent_run_id=data["parent_run_id"],
        case_id=data["case_id"], question=data["question"],
        as_of=datetime.fromisoformat(data["as_of"]),
        perspective_id=data["perspective_id"],
        evidence_bundle_hash=data["evidence_bundle_hash"],
        interpretation=interpretation, status=data["status"],
        errors=tuple(DebateFailure(**item) for item in data["errors"]),
        warnings=tuple(data["warnings"]),
        completed_at=None if data["completed_at"] is None else datetime.fromisoformat(data["completed_at"]),
    )


class DebateLedger:
    """Persist one debate result per immutable input hash."""

    def __init__(self, paths: PathConfig | str | Path):
        if isinstance(paths, PathConfig):
            db_path = paths.data_dir / "index.db"
        else:
            db_path = Path(paths)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        with sqlite3.connect(self._db_path) as db:
            db.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                    run_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL UNIQUE,
                    case_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    profiles_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fallback_reason TEXT,
                    evidence_bundle_hash TEXT NOT NULL,
                    completed_at TEXT,
                    rounds_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )"""
            )
            db.execute(
                f"""CREATE TABLE IF NOT EXISTS {FOLLOW_UP_TABLE} (
                    follow_up_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL UNIQUE,
                    parent_run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    perspective_id TEXT NOT NULL,
                    evidence_bundle_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )"""
            )
        db.close()

    def find(self, input_hash: str) -> DebateResult | None:
        with sqlite3.connect(self._db_path) as db:
            row = db.execute(
                f"SELECT result_json FROM {TABLE} WHERE input_hash = ?",
                (input_hash,),
            ).fetchone()
        db.close()
        if row is None:
            return None
        return replace(result_from_dict(json.loads(row[0])), replayed=True)

    def record(
        self,
        result: DebateResult,
        rounds: list[dict[str, Any]],
        input_hash: str,
    ) -> DebateResult:
        if not isinstance(result, DebateResult):
            raise TypeError("result must be a DebateResult")
        result_json = _json(result_to_dict(result))
        with sqlite3.connect(self._db_path) as db:
            db.execute(
                f"""INSERT OR IGNORE INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    input_hash,
                    input_hash,
                    result.case_id,
                    result.question,
                    result.as_of.isoformat(),
                    _json(list(result.profiles)),
                    result.status,
                    result.fallback_reason,
                    result.evidence_bundle_hash,
                    None
                    if result.completed_at is None
                    else result.completed_at.isoformat(),
                    _json(rounds),
                    result_json,
                ),
            )
        db.close()
        return result

    def result_by_run_id(self, run_id: str) -> DebateResult | None:
        with sqlite3.connect(self._db_path) as db:
            row = db.execute(
                f"SELECT result_json FROM {TABLE} WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else result_from_dict(json.loads(row[0]))

    def find_follow_up(self, input_hash: str) -> FollowUpResult | None:
        with sqlite3.connect(self._db_path) as db:
            row = db.execute(
                f"SELECT result_json FROM {FOLLOW_UP_TABLE} WHERE input_hash = ?",
                (input_hash,),
            ).fetchone()
        return None if row is None else _follow_up_from_dict(json.loads(row[0]))

    def record_follow_up(self, result: FollowUpResult, input_hash: str) -> FollowUpResult:
        if not isinstance(result, FollowUpResult):
            raise TypeError("result must be a FollowUpResult")
        payload = _json(_follow_up_to_dict(result))
        with sqlite3.connect(self._db_path) as db:
            db.execute(
                f"INSERT OR IGNORE INTO {FOLLOW_UP_TABLE} VALUES (?,?,?,?,?,?,?,?,?)",
                (result.follow_up_id, input_hash, result.parent_run_id, result.case_id,
                 result.question, result.as_of.isoformat(), result.perspective_id,
                 result.evidence_bundle_hash, payload),
            )
            row = db.execute(
                f"SELECT result_json FROM {FOLLOW_UP_TABLE} WHERE input_hash = ?",
                (input_hash,),
            ).fetchone()
        return _follow_up_from_dict(json.loads(row[0]))

    def follow_up_entries(self, parent_run_id: str | None = None) -> tuple[FollowUpResult, ...]:
        query = f"SELECT result_json FROM {FOLLOW_UP_TABLE}"
        args: tuple[Any, ...] = ()
        if parent_run_id is not None:
            query += " WHERE parent_run_id = ?"
            args = (parent_run_id,)
        query += " ORDER BY follow_up_id"
        with sqlite3.connect(self._db_path) as db:
            rows = db.execute(query, args).fetchall()
        return tuple(_follow_up_from_dict(json.loads(row[0])) for row in rows)

    def entries(self, case_id: str | None = None) -> tuple[DebateAuditEntry, ...]:
        query = f"SELECT * FROM {TABLE}"
        args: tuple[Any, ...] = ()
        if case_id is not None:
            query += " WHERE case_id = ?"
            args = (case_id,)
        query += " ORDER BY completed_at, run_id"
        with sqlite3.connect(self._db_path) as db:
            rows = db.execute(query, args).fetchall()
        db.close()
        return tuple(
            DebateAuditEntry(
                run_id=row[0],
                input_hash=row[1],
                case_id=row[2],
                question=row[3],
                as_of=datetime.fromisoformat(row[4]),
                profiles=tuple(json.loads(row[5])),
                status=row[6],
                fallback_reason=row[7],
                evidence_bundle_hash=row[8],
                completed_at=None if row[9] is None else datetime.fromisoformat(row[9]),
                rounds_json=row[10],
                result_json=row[11],
            )
            for row in rows
        )

    def close(self) -> None:
        """Compatibility no-op; SQLite connections are scoped per operation."""
