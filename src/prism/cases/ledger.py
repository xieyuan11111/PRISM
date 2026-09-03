"""Durable accumulation of successful per-material case extractions.

The automatic evolution pipeline accumulates, for one ``case_id``, every
material whose structured extraction succeeded and whose evidence-bound
candidates were accepted.  :class:`CaseExtractionLedger` is that accumulation
made durable: each row binds ``(case_id, material_id)`` to the exact
:class:`~prism.domain.Material` and
:class:`~prism.extraction.ExtractionResult` that produced it, so a restarted
process can rebuild the identical merged case bundle from local PRISM data
alone — no LLM, no network, no re-extraction.

Design notes
------------
* The ledger table lives in the SAME SQLite database file as the
  EvidenceStore text index (``index.db`` under ``PathConfig.data_dir``), one
  PRISM home keeps one SQLite file.  The table is created additively
  (``CREATE TABLE IF NOT EXISTS``) so older databases migrate in place.
* Rows are only written after the corresponding merge-and-write succeeded
  (see :class:`prism.cases.service.CaseService`); a failed merge rolls the
  row back, so the ledger never contains a material that poisoned its case.
* Only domain data is stored — never credentials, hosts or absolute paths
  beyond the material's own project-relative ``raw_path``/evidence fields.
* Importing this module touches no network and imports no optional extras.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, is_dataclass
from datetime import datetime, timezone
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from prism.config import PathConfig
from prism.domain import Material
from prism.extraction import ExtractionResult
from prism.store.service import DB_FILENAME

#: Table holding one row per successfully merged (case, material) extraction.
TABLE = "case_extraction_ledger"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    case_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    material_json TEXT NOT NULL,
    extraction_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (case_id, material_id)
);
CREATE INDEX IF NOT EXISTS {TABLE}_material_idx
    ON {TABLE} (material_id);
"""

_UPSERT_SQL = f"""
INSERT INTO {TABLE} (
    case_id, material_id, material_json, extraction_json, recorded_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(case_id, material_id) DO UPDATE SET
    material_json = excluded.material_json,
    extraction_json = excluded.extraction_json,
    updated_at = excluded.updated_at
"""


# --------------------------------------------------------------------- codec


_HINT_CACHE: dict[type, dict[str, Any]] = {}


def _hints(model: type) -> dict[str, Any]:
    cached = _HINT_CACHE.get(model)
    if cached is None:
        cached = get_type_hints(model)
        _HINT_CACHE[model] = cached
    return cached


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            name: _encode(getattr(value, name))
            for name in _hints(type(value))
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"cannot encode {type(value).__name__} for the case ledger"
    )


def _decode(hint: Any, value: Any, path: str) -> Any:
    origin = get_origin(hint)
    if origin in (UnionType, Union):
        alternatives = get_args(hint)
        optional = NoneType in alternatives
        if value is None:
            if optional:
                return None
            raise ValueError(f"{path} must not be null")
        concrete = [item for item in alternatives if item is not NoneType]
        if len(concrete) != 1:
            raise TypeError(f"{path} has an unsupported union type")
        return _decode(concrete[0], value, path)
    if hint is datetime:
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"{path} must be an ISO 8601 string")
        candidate = (
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as error:
            raise ValueError(
                f"{path} must be a valid ISO 8601 timestamp"
            ) from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{path} must be timezone-aware")
        return parsed
    if origin is tuple:
        if not isinstance(value, list):
            raise TypeError(f"{path} must be a JSON array")
        inner = get_args(hint)
        if len(inner) == 2 and inner[1] is Ellipsis:
            return tuple(
                _decode(inner[0], item, f"{path}[{index}]")
                for index, item in enumerate(value)
            )
        if len(value) != len(inner):
            raise ValueError(f"{path} must have exactly {len(inner)} item(s)")
        return tuple(
            _decode(inner[index], item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if hint is str:
        if not isinstance(value, str):
            raise TypeError(f"{path} must be a string")
        return value
    if hint is bool:
        if not isinstance(value, bool):
            raise TypeError(f"{path} must be a boolean")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{path} must be an integer")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{path} must be a number")
        return float(value)
    if is_dataclass(hint):
        if not isinstance(value, dict):
            raise TypeError(f"{path} must be a JSON object")
        field_hints = _hints(hint)
        extra = sorted(set(value) - set(field_hints))
        if extra:
            raise ValueError(
                f"{path} has unexpected field(s): {', '.join(extra)}"
            )
        kwargs: dict[str, Any] = {}
        for name, field_hint in field_hints.items():
            if name not in value:
                raise ValueError(f"{path}.{name} is missing")
            kwargs[name] = _decode(field_hint, value[name], f"{path}.{name}")
        try:
            return hint(**kwargs)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{path} is not a valid {hint.__name__}: {error}"
            ) from error
    raise TypeError(f"{path} has an unsupported type {hint!r}")


def _dumps(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"payload is not valid JSON: {error}") from error


def extraction_to_json(extraction: ExtractionResult) -> str:
    """Serialize one extraction to the ledger's canonical JSON form."""
    if not isinstance(extraction, ExtractionResult):
        raise TypeError("extraction must be an ExtractionResult")
    return _dumps(_encode(extraction))


def extraction_from_json(text: str) -> ExtractionResult:
    """Rebuild the exact extraction recorded by :func:`extraction_to_json`."""
    if not isinstance(text, str):
        raise TypeError("text must be a JSON string")
    return _decode(ExtractionResult, _loads(text), "extraction")


def material_to_json(material: Material) -> str:
    """Serialize one material to the ledger's canonical JSON form."""
    if not isinstance(material, Material):
        raise TypeError("material must be a Material")
    return _dumps(_encode(material))


def material_from_json(text: str) -> Material:
    """Rebuild the exact material recorded by :func:`material_to_json`."""
    if not isinstance(text, str):
        raise TypeError("text must be a JSON string")
    return _decode(Material, _loads(text), "material")


# --------------------------------------------------------------------- ledger


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(name: str, value: str) -> datetime:
    parsed = datetime.fromisoformat(
        value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


class MaterialCaseConflict(ValueError):
    """A material's case binding is ambiguous or would become ambiguous.

    Raised in two situations:

    * :meth:`CaseExtractionLedger.case_for_material` finds a legacy material
      accumulated under several cases — the rows stay readable and
      reportable through :meth:`CaseExtractionLedger.case_ids_for_material`,
      but the reverse lookup cannot pick one;
    * the automatic path (:class:`prism.cases.service.CaseService`) is asked
      to record a material under a case different from the one it is already
      bound to — one material binds one case, and the new binding is refused
      before any row or graph write.

    ``case_ids`` lists every case the material is bound to (ordered);
    ``attempted_case`` is the case the automatic path tried to add, when
    applicable.
    """

    def __init__(
        self,
        material_id: str,
        case_ids: tuple[str, ...],
        attempted_case: str | None = None,
    ) -> None:
        material_id = _require_text("material_id", material_id)
        cases = tuple(case_ids)
        if not cases or any(not isinstance(item, str) or not item.strip() for item in cases):
            raise ValueError("case_ids must contain non-empty case ids")
        listed = ", ".join(repr(item) for item in cases)
        if attempted_case is not None:
            attempted_case = _require_text("attempted_case", attempted_case)
            message = (
                f"material {material_id!r} is already bound to case(s) "
                f"{listed}; refusing to bind it to {attempted_case!r} "
                "(one material binds one case)"
            )
        elif len(cases) == 1:
            message = (
                f"material {material_id!r} is bound to case {cases[0]!r}"
            )
        else:
            message = (
                f"material {material_id!r} is bound to several cases: "
                f"{listed}; the binding is ambiguous"
            )
        super().__init__(message)
        self.material_id = material_id
        self.case_ids = cases
        self.attempted_case = attempted_case


@dataclass(frozen=True, slots=True)
class CaseLedgerEntry:
    """One decoded ``(case_id, material_id)`` ledger row."""

    case_id: str
    material_id: str
    material: Material
    extraction: ExtractionResult
    recorded_at: datetime

    def __post_init__(self) -> None:
        _require_text("case_id", self.case_id)
        _require_text("material_id", self.material_id)
        if not isinstance(self.material, Material):
            raise TypeError("material must be a Material")
        if not isinstance(self.extraction, ExtractionResult):
            raise TypeError("extraction must be an ExtractionResult")


class CaseExtractionLedger:
    """SQLite-backed, restart-durable accumulation of case extractions.

    One ledger serves one PRISM environment (one data dir).  ``record``
    returns the payload it replaced so callers can roll a failed merge back
    to the exact previous state; ``entries`` rebuilds decoded domain objects
    in first-recorded order (the first entry's extraction case is the case
    record the merge keeps).
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

    def record(
        self, case_id: str, material: Material, extraction: ExtractionResult
    ) -> tuple[str, str] | None:
        """Upsert one row; return the replaced ``(material_json,
        extraction_json)`` or ``None`` when the row is new."""
        self._validate_row(case_id, material, extraction)
        material_json = material_to_json(material)
        extraction_json = extraction_to_json(extraction)
        now = _now_iso()
        previous = self._connection.execute(
            f"SELECT material_json, extraction_json FROM {TABLE} "
            "WHERE case_id = ? AND material_id = ?",
            (case_id, material.id),
        ).fetchone()
        self._connection.execute(
            _UPSERT_SQL,
            (case_id, material.id, material_json, extraction_json, now, now),
        )
        self._connection.commit()
        return (previous[0], previous[1]) if previous is not None else None

    def record_raw(
        self,
        case_id: str,
        material_id: str,
        material_json: str,
        extraction_json: str,
    ) -> None:
        """Restore one previously valid row verbatim (rollback support)."""
        _require_text("case_id", case_id)
        _require_text("material_id", material_id)
        if not isinstance(material_json, str) or not material_json.strip():
            raise ValueError("material_json must be a non-empty string")
        if not isinstance(extraction_json, str) or not extraction_json.strip():
            raise ValueError("extraction_json must be a non-empty string")
        now = _now_iso()
        self._connection.execute(
            _UPSERT_SQL,
            (case_id, material_id, material_json, extraction_json, now, now),
        )
        self._connection.commit()

    def remove(self, case_id: str, material_id: str) -> bool:
        """Delete one row; report whether a row was actually removed."""
        _require_text("case_id", case_id)
        _require_text("material_id", material_id)
        cursor = self._connection.execute(
            f"DELETE FROM {TABLE} WHERE case_id = ? AND material_id = ?",
            (case_id, material_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def entries(self, case_id: str) -> tuple[CaseLedgerEntry, ...]:
        """Decode every row of one case in first-recorded order."""
        _require_text("case_id", case_id)
        rows = self._connection.execute(
            f"SELECT case_id, material_id, material_json, extraction_json, "
            f"recorded_at FROM {TABLE} WHERE case_id = ? "
            "ORDER BY recorded_at, rowid",
            (case_id,),
        ).fetchall()
        entries: list[CaseLedgerEntry] = []
        for row_case, material_id, material_json, extraction_json, recorded in rows:
            material = material_from_json(material_json)
            extraction = extraction_from_json(extraction_json)
            if material.id != material_id:
                raise ValueError(
                    f"ledger row {row_case!r}/{material_id!r} material id "
                    f"mismatch: {material.id!r}"
                )
            if extraction.case is None or extraction.case.case_id != row_case:
                raise ValueError(
                    f"ledger row {row_case!r}/{material_id!r} does not "
                    "declare that case"
                )
            entries.append(
                CaseLedgerEntry(
                    case_id=row_case,
                    material_id=material_id,
                    material=material,
                    extraction=extraction,
                    recorded_at=_parse_timestamp("recorded_at", recorded),
                )
            )
        return tuple(entries)

    def case_ids_for_material(self, material_id: str) -> tuple[str, ...]:
        """Every case a material is bound to, in case-id order.

        ``()`` for an unbound material.  Legacy ledgers may return several
        ids for one material; every row stays readable and reportable, while
        :meth:`case_for_material` reports that ambiguity as a typed
        :class:`MaterialCaseConflict`.
        """
        _require_text("material_id", material_id)
        rows = self._connection.execute(
            f"SELECT DISTINCT case_id FROM {TABLE} WHERE material_id = ? "
            "ORDER BY case_id",
            (material_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def case_for_material(self, material_id: str) -> str | None:
        """The case a material was accumulated under, or ``None``.

        Raises :class:`MaterialCaseConflict` (a typed, explicit conflict)
        when a legacy ledger binds the material under several cases — the
        rows remain readable through :meth:`case_ids_for_material`.
        """
        _require_text("material_id", material_id)
        cases = self.case_ids_for_material(material_id)
        if not cases:
            return None
        if len(cases) > 1:
            raise MaterialCaseConflict(material_id, cases)
        return cases[0]

    def close(self) -> None:
        """Close the SQLite connection.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._connection.close()

    @staticmethod
    def _validate_row(
        case_id: str, material: Material, extraction: ExtractionResult
    ) -> None:
        _require_text("case_id", case_id)
        if not isinstance(material, Material):
            raise TypeError("material must be a Material")
        if not isinstance(extraction, ExtractionResult):
            raise TypeError("extraction must be an ExtractionResult")
        _require_text("material.id", material.id)
        if extraction.case is None or extraction.case.case_id != case_id:
            raise ValueError(
                f"extraction must declare case {case_id!r}"
            )


__all__ = [
    "CaseExtractionLedger",
    "CaseLedgerEntry",
    "MaterialCaseConflict",
    "extraction_from_json",
    "extraction_to_json",
    "material_from_json",
    "material_to_json",
]
