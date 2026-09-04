"""Immutable, project-owned version ledger for rendered case reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Callable

from prism.analyzer import EvolutionAnalysis
from prism.config import PathConfig
from prism.debate import DebateResult
from prism.report.models import ReportDocument
from prism.report.pdf import ReportPdfExporter, ReportPdfExportResult
from prism.store.service import DB_FILENAME

TABLE = "report_versions"
_HASH_SCHEMA = "prism.report.version.v1"
_TRIGGERS = frozenset(
    {"initial", "material_added", "rebuild", "debate_updated"}
)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    version_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    input_hash TEXT NOT NULL UNIQUE,
    markdown_hash TEXT NOT NULL,
    summary_origin TEXT NOT NULL,
    debate_input_hash TEXT,
    markdown TEXT NOT NULL,
    parent_version_id TEXT,
    trigger_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS {TABLE}_case_idx
    ON {TABLE} (case_id, created_at);
"""

_COLUMNS = (
    "version_id, case_id, as_of, created_at, input_hash, markdown_hash, "
    "summary_origin, debate_input_hash, markdown, parent_version_id, trigger_type"
)


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _parse_timestamp(name: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )


def _json_default(value: object) -> str:
    """Serialize one non-JSON value deterministically.

    Datetimes are normalized to UTC so one cutoff instant hashes identically
    whatever offset representation the caller used; every other value falls
    back to ``str`` (tuples/None are already handled by the encoder).
    """
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _utc_iso(value: datetime) -> str:
    """One aware datetime as a UTC-normalized ISO 8601 string."""
    return value.astimezone(timezone.utc).isoformat()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReportVersion:
    """One immutable report snapshot and its reproducibility metadata."""

    version_id: str
    case_id: str
    as_of: datetime
    created_at: datetime
    input_hash: str
    markdown_hash: str
    summary_origin: str
    debate_input_hash: str | None
    markdown: str
    parent_version_id: str | None
    trigger: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version_id", _require_text("version_id", self.version_id)
        )
        object.__setattr__(
            self, "case_id", _require_text("case_id", self.case_id)
        )
        _require_aware("as_of", self.as_of)
        _require_aware("created_at", self.created_at)
        object.__setattr__(
            self, "input_hash", _require_text("input_hash", self.input_hash)
        )
        object.__setattr__(
            self,
            "markdown_hash",
            _require_text("markdown_hash", self.markdown_hash),
        )
        object.__setattr__(
            self,
            "summary_origin",
            _require_text("summary_origin", self.summary_origin),
        )
        if self.debate_input_hash is not None:
            object.__setattr__(
                self,
                "debate_input_hash",
                _require_text("debate_input_hash", self.debate_input_hash),
            )
        object.__setattr__(
            self, "markdown", _require_text("markdown", self.markdown)
        )
        if self.parent_version_id is not None:
            object.__setattr__(
                self,
                "parent_version_id",
                _require_text("parent_version_id", self.parent_version_id),
            )
        object.__setattr__(
            self, "trigger", _require_text("trigger", self.trigger)
        )
        if self.trigger not in _TRIGGERS:
            allowed = ", ".join(sorted(_TRIGGERS))
            raise ValueError(f"trigger must be one of: {allowed}")


class ReportVersionLedger:
    """Persist immutable report snapshots in PRISM's shared SQLite database."""

    def __init__(
        self,
        paths: PathConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        pdf_exporter: ReportPdfExporter | None = None,
    ) -> None:
        if not isinstance(paths, PathConfig):
            raise TypeError("paths must be a PathConfig")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._paths = paths
        if pdf_exporter is not None and not callable(
            getattr(pdf_exporter, "export_pdf", None)
        ):
            raise TypeError("pdf_exporter must provide export_pdf()")
        self._pdf_exporter = pdf_exporter or ReportPdfExporter(paths)
        database = paths.data_dir / DB_FILENAME
        database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database))
        self._connection.executescript(_DDL)
        self._connection.commit()
        self._closed = False

    def input_hash(
        self,
        analysis: EvolutionAnalysis,
        debate_result: DebateResult | None = None,
    ) -> str:
        """Hash the exact structured report inputs, not their timestamps."""

        if not isinstance(analysis, EvolutionAnalysis):
            raise TypeError("analysis must be an EvolutionAnalysis")
        if debate_result is not None and not isinstance(
            debate_result, DebateResult
        ):
            raise TypeError("debate_result must be a DebateResult")
        return _digest(
            {
                "schema": _HASH_SCHEMA,
                "analysis": asdict(analysis),
                "debate": asdict(debate_result)
                if debate_result is not None
                else None,
            }
        )

    def debate_input_hash(self, debate_result: DebateResult) -> str:
        if not isinstance(debate_result, DebateResult):
            raise TypeError("debate_result must be a DebateResult")
        return _digest(
            {"schema": _HASH_SCHEMA, "debate": asdict(debate_result)}
        )

    def markdown_hash(self, markdown: str) -> str:
        _require_text("markdown", markdown)
        return hashlib.sha256(markdown.encode("utf-8")).hexdigest()

    def save(
        self,
        document: ReportDocument,
        analysis: EvolutionAnalysis,
        *,
        trigger: str = "initial",
        debate_result: DebateResult | None = None,
    ) -> ReportVersion:
        """Save one version; identical input always returns the existing row."""

        if not isinstance(document, ReportDocument):
            raise TypeError("document must be a ReportDocument")
        if not isinstance(analysis, EvolutionAnalysis):
            raise TypeError("analysis must be an EvolutionAnalysis")
        if document.case_id != analysis.case_id or document.as_of != analysis.as_of:
            raise ValueError("document must match the analysis case and cutoff")
        if debate_result is not None and document.debate is not debate_result:
            raise ValueError("document must carry the supplied debate result")

        digest = self.input_hash(analysis, debate_result)
        existing = self.find_by_input_hash(digest)
        if existing is not None:
            return existing

        markdown_hash = self.markdown_hash(document.markdown)
        parent = self.latest(document.case_id)
        record = ReportVersion(
            version_id=f"rv_{digest}",
            case_id=document.case_id,
            as_of=document.as_of,
            created_at=self._clock(),
            input_hash=digest,
            markdown_hash=markdown_hash,
            summary_origin=document.summary.origin,
            debate_input_hash=(
                self.debate_input_hash(debate_result)
                if debate_result is not None
                else None
            ),
            markdown=document.markdown,
            parent_version_id=parent.version_id if parent is not None else None,
            trigger=trigger,
        )
        try:
            self._connection.execute(
                f"INSERT INTO {TABLE} ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.version_id,
                    record.case_id,
                    _utc_iso(record.as_of),
                    record.created_at.isoformat(),
                    record.input_hash,
                    record.markdown_hash,
                    record.summary_origin,
                    record.debate_input_hash,
                    record.markdown,
                    record.parent_version_id,
                    record.trigger,
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError:
            existing = self.find_by_input_hash(digest)
            if existing is not None:
                return existing
            raise
        return record

    def find_by_input_hash(self, input_hash: str) -> ReportVersion | None:
        _require_text("input_hash", input_hash)
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE input_hash = ?",
            (input_hash,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def get(self, version_id: str) -> ReportVersion | None:
        _require_text("version_id", version_id)
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def export_pdf(
        self, version_id: str, output_path: str | Path
    ) -> ReportPdfExportResult:
        """Export one saved report version as a derived PDF."""

        version = self.get(version_id)
        if version is None:
            raise LookupError(f"no report version {version_id!r}")
        return self._pdf_exporter.export_version(version, output_path)

    def versions(
        self,
        case_id: str | None = None,
        *,
        as_of: datetime | None = None,
    ) -> tuple[ReportVersion, ...]:
        """List versions in creation order, optionally scoped to one cutoff."""

        if case_id is not None:
            _require_text("case_id", case_id)
        if as_of is not None:
            _require_aware("as_of", as_of)
        clauses: list[str] = []
        parameters: list[object] = []
        if case_id is not None:
            clauses.append("case_id = ?")
            parameters.append(case_id)
        if as_of is not None:
            clauses.append("as_of = ?")
            parameters.append(_utc_iso(as_of))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM {TABLE}{where} "
            "ORDER BY created_at, rowid",
            parameters,
        ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def latest(self, case_id: str) -> ReportVersion | None:
        _require_text("case_id", case_id)
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE case_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()

    @staticmethod
    def _decode(row: tuple) -> ReportVersion:
        return ReportVersion(
            version_id=row[0],
            case_id=row[1],
            as_of=_parse_timestamp("as_of", row[2]),
            created_at=_parse_timestamp("created_at", row[3]),
            input_hash=row[4],
            markdown_hash=row[5],
            summary_origin=row[6],
            debate_input_hash=row[7],
            markdown=row[8],
            parent_version_id=row[9],
            trigger=row[10],
        )


__all__ = ["ReportVersion", "ReportVersionLedger", "TABLE"]
