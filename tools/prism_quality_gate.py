#!/usr/bin/env python3
"""Offline quality-gate metrics for one PRISM RC run.

Reads only local, already-produced artifacts — a JSON run summary and the
PRISM SQLite index (``index.db``) — and writes a sanitized, structured
quality report to a caller-specified location.  The tool is stdlib-only,
never calls an LLM or the network, opens the index strictly read-only,
and never copies absolute paths, secrets or material content into the
report: every value is a count, a rate, a closed-vocabulary label or a
constructed verdict string.

Input contract
--------------
``--run-summary`` points at the run summary JSON.  The reader is
deliberately tolerant because RC harnesses evolve; accepted shapes:

* ``input_files``: an integer, or a list (its length is used); also
  accepted as ``materials.input_files``.
* ``successful`` / ``failed``: an integer, or a list (length); also
  accepted as ``materials.successful`` / ``materials.failed``.
* target case ids: ``case_ids`` / ``case_id`` (or ``case.case_id``).

``--index-db`` points at the PRISM ``index.db``.  Required tables are
``documents`` and ``case_extraction_ledger``; ``pipeline_outcomes`` and
``material_evidence_ledger`` are optional and their absence degrades the
report instead of failing it.  Ledger ``extraction_json`` payloads are
read as plain JSON (no domain decoding), so the tool also tolerates rows
written by older PRISM versions.

``--run-dir`` discovers ``run-summary.json`` (or ``run_summary.json``)
and ``index.db`` in the directory itself or under its ``data``
subdirectory.

Verdict policy
--------------
``verdict.mechanism_status`` and ``verdict.semantic_status`` are each one
of ``pass`` / ``partial`` / ``fail``, always accompanied by ``reasons``.
The gate is conservative by construction: pipeline success alone never
yields a semantic pass — a semantic pass additionally requires substantive
extracted candidates, zero evidence gaps, full citation and locator
coverage, fully resolved citations and no validated-but-unbound materials.

Exit codes: ``0`` report produced (verdicts are data, not CLI errors);
``1`` ``--strict`` with any non-pass verdict; ``2`` usage/input errors;
``3`` structural data or sanitization errors.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = 1
TOOL_NAME = "prism-quality-gate"

RUN_SUMMARY_NAMES = ("run-summary.json", "run_summary.json")
REQUIRED_TABLES = ("documents", "case_extraction_ledger")
KNOWN_GAP_TYPES = ("candidate_validation_failed", "evidence_location_failed")
RELATION_TYPES = ("supersedes", "revises", "contradicts", "triggered_by")

_KINDS = (
    ("nodes", "node"),
    ("temporal_facts", "temporal_fact"),
    ("claims", "claim"),
    ("conflicts", "conflict"),
    ("relations", "relation"),
)

# Labels (case ids, distribution keys) must stay opaque, portable and
# provably path-free before they may appear in a public report.
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Every string in the finished report is checked against these patterns
# before the report leaves the process; a hit is a sanitization failure,
# never a silent redaction.
_PRIVACY_PATTERNS = (
    ("windows-drive-path", re.compile(r"[A-Za-z]:[/\\]")),
    ("unc-path", re.compile(r"\\\\")),
    ("posix-home-path", re.compile(r"(?<![\w.])/(?:home|Users|root|tmp|var)/")),
    (
        "secret-like-wording",
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|credential|password|passwd"
            r"|secret|token|bearer)\b"
        ),
    ),
)


class QualityGateError(Exception):
    """Operational failure with an explicit, user-facing classification."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# ------------------------------------------------------------------- sanitizing


def _safe_label(value: object, fallback: str) -> str:
    """Return ``value`` when it is a safe opaque label, else ``fallback``."""
    if isinstance(value, str) and _SAFE_LABEL.match(value):
        return value
    return fallback


def _label_map(values: list[str]) -> dict[str, str]:
    """Unique safe labels for raw ids; colliding raw ids get suffixes."""
    labels: dict[str, str] = {}
    used: dict[str, int] = {}
    for raw in values:
        label = _safe_label(raw, "unsafe-id")
        used[label] = used.get(label, 0) + 1
        labels[raw] = label if used[label] == 1 else f"{label}-{used[label]}"
    return labels


def _dist_add(dist: dict[str, int], value: object) -> None:
    if value is None:
        key = "unset"
    elif isinstance(value, str) and _SAFE_LABEL.match(value):
        key = value
    else:
        key = "other"
    dist[key] = dist.get(key, 0) + 1


def _has_text(values: object) -> bool:
    return isinstance(values, list) and any(
        isinstance(item, str) and item.strip() for item in values
    )


def _guard_sanitized(report: dict[str, Any], forbidden_fragments: set[str]) -> None:
    """Refuse to emit any report string that leaks paths or secret wording."""

    problems: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                visit(key)
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, str):
            for name, pattern in _PRIVACY_PATTERNS:
                if pattern.search(node):
                    problems.add(name)
            for fragment in forbidden_fragments:
                if fragment and fragment in node:
                    problems.add("input-path-fragment")

    visit(report)
    if problems:
        raise QualityGateError(
            "sanitization",
            "report strings matched privacy guard patterns: "
            + ", ".join(sorted(problems)),
        )


# --------------------------------------------------------------------- inputs


def _discover_run_dir(run_dir: Path) -> tuple[Path, Path]:
    """Locate the run summary and index db inside one run directory."""
    if not run_dir.is_dir():
        raise QualityGateError("input", f"run directory not found: {run_dir}")
    summary: Path | None = None
    index: Path | None = None
    for base in (run_dir, run_dir / "data"):
        if index is None and (base / "index.db").is_file():
            index = base / "index.db"
        for name in RUN_SUMMARY_NAMES:
            if summary is None and (base / name).is_file():
                summary = base / name
    missing = []
    if summary is None:
        missing.append("run summary (" + "/".join(RUN_SUMMARY_NAMES) + ")")
    if index is None:
        missing.append("index db (index.db)")
    if missing:
        raise QualityGateError(
            "input",
            f"run directory does not contain {' or '.join(missing)}: {run_dir}",
        )
    assert summary is not None and index is not None
    return summary, index


def _load_run_summary(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QualityGateError("input", f"cannot read run summary: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QualityGateError(
            "data", f"run summary is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise QualityGateError("data", "run summary must be a JSON object")
    return payload


def _count_like(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    return None


def _run_summary_counts(payload: dict[str, Any] | None) -> dict[str, int | None]:
    if not isinstance(payload, dict):
        return {"input_files": None, "successful": None, "failed": None}
    materials = payload.get("materials")
    nested = materials if isinstance(materials, dict) else {}
    return {
        "input_files": _count_like(
            nested.get("input_files", payload.get("input_files"))
        ),
        "successful": _count_like(
            nested.get("successful", payload.get("successful"))
        ),
        "failed": _count_like(nested.get("failed", payload.get("failed"))),
    }


def _run_summary_case_ids(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    for key in ("case_ids", "target_case_ids"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            ids = [item for item in value if isinstance(item, str) and item.strip()]
            if ids:
                return ids
    for key in ("case_id", "target_case_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return [value]
    case = payload.get("case")
    if isinstance(case, dict):
        value = case.get("case_id")
        if isinstance(value, str) and value.strip():
            return [value]
    return []


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise QualityGateError("input", f"index db file not found: {path}")
    uri = "file:" + quote(path.resolve().as_posix()) + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise QualityGateError("data", f"cannot open index db: {exc}") from exc


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise QualityGateError(
            "data", f"cannot read index db schema: {exc}"
        ) from exc
    return row is not None


# ------------------------------------------------------------------ statistics


class _Stats:
    """Mutable accumulation over decoded ledger extraction payloads."""

    def __init__(self) -> None:
        self.substantive = {
            "nodes": 0,
            "temporal_facts": 0,
            "claims": 0,
            "conflicts": 0,
            "relations": 0,
        }
        self.node_type: dict[str, int] = {}
        self.claim_type: dict[str, int] = {}
        self.evidence_role = {
            "all": {},
            "node": {},
            "temporal_fact": {},
            "claim": {},
            "conflict": {},
            "relation": {},
        }
        self.cited = 0
        self.located = 0
        self.cited_ids: set[str] = set()
        self.gaps = {
            "candidate_validation_failed": 0,
            "evidence_location_failed": 0,
            "other": 0,
        }
        self.other_gap_types: dict[str, int] = {}
        self.facts_invalidated = 0
        self.claims_revised = 0
        self.relations = {name: 0 for name in RELATION_TYPES}
        self.relations["other"] = 0

    def accumulate(self, payload: dict[str, Any]) -> None:
        for key, kind in _KINDS:
            items = payload.get(key)
            if not isinstance(items, list):
                continue
            self.substantive[key] += len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                refs = item.get("based_on" if kind == "claim" else "source_ids")
                if _has_text(refs):
                    self.cited += 1
                    for ref in refs:
                        if isinstance(ref, str) and ref.strip():
                            self.cited_ids.add(ref)
                evidence = item.get("evidence")
                if isinstance(evidence, list) and evidence:
                    self.located += 1
                role = item.get("evidence_role")
                _dist_add(self.evidence_role["all"], role)
                _dist_add(self.evidence_role[kind], role)
                if kind == "node":
                    _dist_add(self.node_type, item.get("node_type"))
                elif kind == "claim":
                    _dist_add(self.claim_type, item.get("claim_type"))
                    revised = item.get("revised_by")
                    if isinstance(revised, str) and revised.strip():
                        self.claims_revised += 1
                elif kind == "temporal_fact":
                    if item.get("invalid_at"):
                        self.facts_invalidated += 1
                elif kind == "relation":
                    relation_type = item.get("relation_type")
                    if relation_type in self.relations:
                        self.relations[relation_type] += 1
                    else:
                        self.relations["other"] += 1
        gaps = payload.get("evidence_gaps")
        if isinstance(gaps, list):
            for gap in gaps:
                gap_type = gap.get("gap_type") if isinstance(gap, dict) else None
                if gap_type in KNOWN_GAP_TYPES:
                    self.gaps[gap_type] += 1
                else:
                    self.gaps["other"] += 1
                    _dist_add(self.other_gap_types, gap_type)

    @property
    def substantive_total(self) -> int:
        return sum(self.substantive.values())

    @property
    def gap_total(self) -> int:
        return sum(self.gaps.values())


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _coverage(
    numerator: int, denominator: int, definition: str
) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": _rate(numerator, denominator),
        "definition": definition,
    }


# ------------------------------------------------------------------- reporting


def build_report(
    conn: sqlite3.Connection,
    run_summary: dict[str, Any] | None,
    case_ids: list[str],
) -> dict[str, Any]:
    """Assemble the sanitized quality report for the target case ids."""
    outcomes_present = _table_exists(conn, "pipeline_outcomes")
    material_ledger_present = _table_exists(conn, "material_evidence_ledger")

    committed = failed_outcomes = 0
    failed_by_stage: dict[str, int] = {}
    if outcomes_present:
        for status, count in conn.execute(
            "SELECT status, COUNT(*) FROM pipeline_outcomes GROUP BY status"
        ):
            if status == "committed":
                committed = count
            elif status == "failed":
                failed_outcomes = count
        for stage, count in conn.execute(
            "SELECT stage, COUNT(*) FROM pipeline_outcomes"
            " WHERE status = 'failed' GROUP BY stage"
        ):
            key = "unknown" if stage is None else _safe_label(stage, "other")
            failed_by_stage[key] = failed_by_stage.get(key, 0) + count

    summary_counts = _run_summary_counts(run_summary)
    if outcomes_present:
        successful: int | None = committed
        failed: int | None = failed_outcomes
        counts_source = "pipeline_outcomes"
    elif (
        summary_counts["successful"] is not None
        or summary_counts["failed"] is not None
    ):
        successful = summary_counts["successful"]
        failed = summary_counts["failed"]
        counts_source = "run_summary"
    else:
        successful = None
        failed = None
        counts_source = None

    document_ids = {
        str(row[0])
        for row in conn.execute("SELECT source_id FROM documents")
    }

    labels = _label_map(case_ids)
    placeholders = ", ".join("?" for _ in case_ids)
    try:
        ledger_rows = conn.execute(
            "SELECT case_id, material_id, extraction_json"
            f" FROM case_extraction_ledger WHERE case_id IN ({placeholders})"
            " ORDER BY recorded_at, rowid",
            case_ids,
        ).fetchall()
        cases_in_ledger = conn.execute(
            "SELECT COUNT(DISTINCT case_id) FROM case_extraction_ledger"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        raise QualityGateError(
            "data", f"cannot read the case extraction ledger: {exc}"
        ) from exc

    stats = _Stats()
    rows_by_case: dict[str, int] = {}
    ledger_materials: set[str] = set()
    unreadable_rows = 0
    for row_case, material_id, extraction_json in ledger_rows:
        label = labels.get(row_case, "unsafe-id")
        rows_by_case[label] = rows_by_case.get(label, 0) + 1
        if isinstance(material_id, str) and material_id.strip():
            ledger_materials.add(material_id)
        try:
            payload = json.loads(extraction_json)
        except (TypeError, json.JSONDecodeError):
            unreadable_rows += 1
            continue
        if not isinstance(payload, dict):
            unreadable_rows += 1
            continue
        stats.accumulate(payload)

    awaiting_rows = 0
    awaiting_substantive = 0
    awaiting_gaps = 0
    if material_ledger_present:
        try:
            for (extraction_json,) in conn.execute(
                "SELECT extraction_json FROM material_evidence_ledger"
            ):
                awaiting_rows += 1
                try:
                    payload = json.loads(extraction_json)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                awaiting = _Stats()
                awaiting.accumulate(payload)
                awaiting_substantive += awaiting.substantive_total
                awaiting_gaps += awaiting.gap_total
        except sqlite3.Error as exc:
            raise QualityGateError(
                "data", f"cannot read the material evidence ledger: {exc}"
            ) from exc

    ledger_rows_total = len(ledger_rows)
    materials_missing_from_index = len(ledger_materials - document_ids)
    cited_resolved = len(stats.cited_ids & document_ids)
    cited_total = len(stats.cited_ids)
    substantive_total = stats.substantive_total

    # ---------------------------------------------------------------- verdict
    mechanism_reasons: list[str] = []
    mechanism_fail = False
    semantic_reasons: list[str] = []
    semantic_fail = False

    if counts_source is None:
        mechanism_fail = True
        mechanism_reasons.append(
            "no material count source available (run summary absent and"
            " pipeline outcomes table missing)"
        )
    else:
        if run_summary is None:
            mechanism_reasons.append(
                "run summary not provided; material counts rely on the"
                " index ledger alone"
            )
        if not outcomes_present:
            mechanism_reasons.append(
                "pipeline outcomes table missing; material counts rely on"
                " the run summary alone"
            )
    if failed:
        mechanism_reasons.append(
            f"{failed} material(s) failed pipeline processing"
        )
    if outcomes_present and run_summary is not None:
        for name, summary_value, ledger_value in (
            ("successful", summary_counts["successful"], committed),
            ("failed", summary_counts["failed"], failed_outcomes),
        ):
            if summary_value is not None and summary_value != ledger_value:
                mechanism_reasons.append(
                    f"run summary {name} count ({summary_value}) disagrees"
                    f" with committed outcomes ({ledger_value})"
                )

    if not case_ids:
        mechanism_fail = True
        mechanism_reasons.append(
            "no target case ids could be determined"
        )
    elif ledger_rows_total == 0:
        mechanism_fail = True
        mechanism_reasons.append(
            "no committed ledger rows for the target case(s)"
        )
    elif unreadable_rows == ledger_rows_total:
        mechanism_fail = True
        mechanism_reasons.append(
            f"all {unreadable_rows} ledger row(s) could not be parsed"
        )
    elif unreadable_rows:
        mechanism_reasons.append(
            f"{unreadable_rows} ledger row(s) could not be parsed"
        )
    empty_targets = [
        label for label in map(labels.get, case_ids)  # type: ignore[arg-type]
        if label is not None and not rows_by_case.get(label)
    ]
    if empty_targets and ledger_rows_total:
        for label in dict.fromkeys(empty_targets):
            mechanism_reasons.append(f"target case {label} has no ledger rows")
    if materials_missing_from_index:
        mechanism_reasons.append(
            f"{materials_missing_from_index} ledger material(s) are missing"
            " from the documents index"
        )

    if substantive_total == 0:
        semantic_fail = True
        semantic_reasons.append("no substantive extracted candidates")
    if stats.gap_total:
        semantic_reasons.append(
            "evidence gaps: {total} (candidate_validation_failed={cvf},"
            " evidence_location_failed={elf}, other={other})".format(
                total=stats.gap_total,
                cvf=stats.gaps["candidate_validation_failed"],
                elf=stats.gaps["evidence_location_failed"],
                other=stats.gaps["other"],
            )
        )
    for name, numerator, denominator in (
        ("source id citation coverage", stats.cited, substantive_total),
        ("evidence locator coverage", stats.located, substantive_total),
    ):
        if denominator and numerator < denominator:
            semantic_reasons.append(f"{name} {numerator}/{denominator}")
    if cited_total and cited_resolved < cited_total:
        semantic_reasons.append(
            f"{cited_total - cited_resolved} cited source id(s) missing"
            " from the documents index"
        )
    if awaiting_rows:
        semantic_reasons.append(
            f"{awaiting_rows} validated material extraction(s) await"
            " explicit case binding"
        )

    mechanism_status = (
        "fail" if mechanism_fail else ("partial" if mechanism_reasons else "pass")
    )
    semantic_status = (
        "fail" if semantic_fail else ("partial" if semantic_reasons else "pass")
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "run_summary_present": run_summary is not None,
            "pipeline_outcomes_present": outcomes_present,
            "material_evidence_ledger_present": material_ledger_present,
        },
        "materials": {
            "input_files": summary_counts["input_files"],
            "successful": successful,
            "failed": failed,
            "counts_source": counts_source,
            "failed_by_stage": failed_by_stage,
        },
        "cases": {
            "target_case_ids": [labels.get(case_id, "unsafe-id") for case_id in case_ids],
            "case_ids_in_ledger": cases_in_ledger,
            "ledger_rows": ledger_rows_total,
            "rows_by_case": rows_by_case,
            "distinct_materials": len(ledger_materials),
            "unreadable_rows": unreadable_rows,
        },
        "substantive": {
            **stats.substantive,
            "total": substantive_total,
        },
        "distributions": {
            "node_type": stats.node_type,
            "claim_type": stats.claim_type,
            "evidence_role": stats.evidence_role,
        },
        "coverage": {
            "source_ids": _coverage(
                stats.cited,
                substantive_total,
                "substantive items citing at least one source id"
                " (claims use based_on)",
            ),
            "evidence_locator": _coverage(
                stats.located,
                substantive_total,
                "substantive items carrying at least one evidence locator",
            ),
            "cited_source_ids_resolved": _coverage(
                cited_resolved,
                cited_total,
                "distinct cited source ids present in the documents index",
            ),
        },
        "evidence_gaps": {
            **stats.gaps,
            "other_types": stats.other_gap_types,
            "total": stats.gap_total,
        },
        "evolution": {
            "facts_invalidated": stats.facts_invalidated,
            "claims_revised": stats.claims_revised,
            "relations_supersedes": stats.relations["supersedes"],
            "relations_revises": stats.relations["revises"],
            "relations_contradicts": stats.relations["contradicts"],
            "relations_triggered_by": stats.relations["triggered_by"],
        },
        "awaiting_case_binding": {
            "rows": awaiting_rows,
            "substantive_items": awaiting_substantive,
            "evidence_gaps": awaiting_gaps,
        },
        "verdict": {
            "mechanism_status": mechanism_status,
            "semantic_status": semantic_status,
            "reasons": mechanism_reasons + semantic_reasons,
        },
    }


# ------------------------------------------------------------------------ CLI


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Offline, sanitized quality report for one PRISM RC run.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--run-dir",
        help="run directory holding run-summary.json and index.db"
        " (or data/index.db)",
    )
    source.add_argument("--index-db", help="path to the PRISM index.db")
    parser.add_argument("--run-summary", help="path to the run summary JSON")
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="target case id; repeatable (default: run summary, then all"
        " ledger cases)",
    )
    parser.add_argument(
        "--output", help="write the JSON report here (default: stdout)"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when any verdict is not pass",
    )
    parser.add_argument(
        "--indent", action="store_true", help="pretty-print the JSON report"
    )
    args = parser.parse_args(argv)
    if args.run_dir and args.run_summary:
        parser.error(
            "use either --run-dir or --run-summary/--index-db, not both"
        )
    if args.case_ids:
        for case_id in args.case_ids:
            if not case_id.strip():
                raise QualityGateError(
                    "usage", "case id must be a non-empty string"
                )
    return args


def _resolve_case_ids(
    cli_ids: list[str] | None,
    run_summary: dict[str, Any] | None,
    conn: sqlite3.Connection,
) -> list[str]:
    if cli_ids:
        return cli_ids
    hinted = _run_summary_case_ids(run_summary)
    if hinted:
        return hinted
    try:
        rows = conn.execute(
            "SELECT DISTINCT case_id FROM case_extraction_ledger ORDER BY case_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise QualityGateError(
            "data", f"cannot list ledger case ids: {exc}"
        ) from exc
    return [str(row[0]) for row in rows]


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        if args.run_dir is not None:
            summary_path, index_path = _discover_run_dir(Path(args.run_dir))
        else:
            assert args.index_db is not None
            index_path = Path(args.index_db)
            summary_path = (
                Path(args.run_summary) if args.run_summary else None
            )
        run_summary = _load_run_summary(summary_path) if summary_path else None

        conn = _connect_readonly(index_path)
        try:
            missing_tables = [
                name for name in REQUIRED_TABLES if not _table_exists(conn, name)
            ]
            if missing_tables:
                raise QualityGateError(
                    "data",
                    "index db is missing required table(s): "
                    + ", ".join(missing_tables),
                )
            case_ids = _resolve_case_ids(args.case_ids, run_summary, conn)
            report = build_report(conn, run_summary, case_ids)
        finally:
            conn.close()
    except QualityGateError as exc:
        print(f"{exc.kind}-error: {exc.message}", file=sys.stderr)
        return 2 if exc.kind in ("usage", "input") else 3

    forbidden: set[str] = set()
    for path in (summary_path, index_path):
        if path is not None:
            forbidden.update(
                {str(path), str(path.resolve()), path.resolve().as_posix()}
            )
    if args.output:
        output_path = Path(args.output)
        forbidden.update(
            {str(output_path), str(output_path.resolve())}
        )
    if args.run_dir:
        run_dir = Path(args.run_dir)
        forbidden.update({str(run_dir), str(run_dir.resolve())})
    _guard_sanitized(report, forbidden)

    rendered = json.dumps(
        report,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        indent=2 if args.indent else None,
    )
    if args.output:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    else:
        sys.stdout.write(rendered + "\n")
    verdict = report["verdict"]
    print(
        f"{TOOL_NAME}: mechanism={verdict['mechanism_status']}"
        f" semantic={verdict['semantic_status']}",
        file=sys.stderr,
    )
    if args.strict and (
        verdict["mechanism_status"] != "pass"
        or verdict["semantic_status"] != "pass"
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
