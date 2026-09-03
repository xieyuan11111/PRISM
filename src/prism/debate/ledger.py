"""Minimal SQLite audit ledger for automatic debate runs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from prism.config import PathConfig

from .models import DebateResult, result_from_dict, result_to_dict

TABLE = "debate_audit"


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
