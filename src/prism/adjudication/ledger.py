from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from prism.config import PathConfig
from .models import AuditRecord, AdjudicationDecision

TABLE = "adjudication_audit"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AdjudicationLedger:
    def __init__(self, paths: PathConfig | str | Path):
        if isinstance(paths, PathConfig):
            db_path = paths.data_dir / "index.db"
        else:
            db_path = Path(paths)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        with sqlite3.connect(self._db_path) as db:
            db.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                decision_id TEXT PRIMARY KEY,
                material_id TEXT NOT NULL,
                candidate_kind TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                original_payload_hash TEXT NOT NULL,
                original_payload_json TEXT NOT NULL,
                validation_failures_json TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                revised_payload_json TEXT,
                model_role TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                revalidation_outcome TEXT NOT NULL,
                graph_episode_keys_json TEXT NOT NULL
            )""")

    def record(self, record: AuditRecord) -> AuditRecord:
        with sqlite3.connect(self._db_path) as db:
            db.execute(f"""INSERT OR IGNORE INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                record.decision_id, record.material_id, record.candidate_kind,
                record.candidate_id, record.original_payload_hash,
                _json(dict(record.original_payload)), _json(list(record.validation_failures)),
                record.decision.value,
                record.reason,
                None if record.revised_payload is None else _json(dict(record.revised_payload)),
                record.model_role, record.decided_at.isoformat(), record.revalidation_outcome,
                _json(list(record.graph_episode_keys)),
            ))
        return record

    def entries(self, material_id: str | None = None) -> tuple[AuditRecord, ...]:
        query = f"SELECT * FROM {TABLE}"
        args: tuple[Any, ...] = ()
        if material_id is not None:
            query += " WHERE material_id = ?"
            args = (material_id,)
        query += " ORDER BY decided_at, decision_id"
        with sqlite3.connect(self._db_path) as db:
            rows = db.execute(query, args).fetchall()
        result = []
        for row in rows:
            result.append(AuditRecord(
                decision_id=row[0], material_id=row[1], candidate_kind=row[2], candidate_id=row[3],
                original_payload_hash=row[4], original_payload=json.loads(row[5]),
                validation_failures=tuple(json.loads(row[6])), decision=AdjudicationDecision(row[7]),
                reason=row[8], revised_payload=None if row[9] is None else json.loads(row[9]),
                model_role=row[10], decided_at=datetime.fromisoformat(row[11]),
                revalidation_outcome=row[12], graph_episode_keys=tuple(json.loads(row[13]))
            ))
        return tuple(result)

    def close(self) -> None:
        """Compatibility no-op; connections are scoped per operation."""
        return None
