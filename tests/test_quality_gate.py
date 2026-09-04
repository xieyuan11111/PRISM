"""Tests for the offline PRISM quality-gate tool (tools/prism_quality_gate.py).

Everything here runs against synthetic, temporary artifacts only: a tiny
SQLite index carrying the PRISM ledger schema and a JSON run summary.  No
network, no LLM, no real-case paths, no private directories.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import prism_quality_gate as gate  # noqa: E402

CASE = "synthetic-case-1"
DT = "2026-09-01T00:00:00+00:00"
DT2 = "2026-09-02T00:00:00+00:00"
UTC = timezone.utc

_SCHEMA = """
CREATE TABLE documents (source_id TEXT PRIMARY KEY);
CREATE TABLE case_extraction_ledger (
    case_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    material_json TEXT NOT NULL,
    extraction_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (case_id, material_id)
);
CREATE TABLE material_evidence_ledger (
    material_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    material_json TEXT NOT NULL,
    extraction_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE pipeline_outcomes (
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


# --------------------------------------------------------------- synthetic JSON


def _locator(source_id: str) -> dict:
    return {
        "source_id": source_id,
        "corpus_path": f"corpus/{source_id}.md",
        "paragraph": 1,
        "page": None,
        "quote": "synthetic locator quote",
    }


def _node(
    node_type: str = "publication",
    source_ids: tuple[str, ...] = ("mat-a",),
    evidence: bool = True,
    evidence_role: str | None = None,
) -> dict:
    return {
        "id": f"node-{node_type}",
        "case_id": CASE,
        "node_type": node_type,
        "happened_at": DT,
        "summary": "synthetic node summary",
        "source_ids": list(source_ids),
        "claim_ids": [],
        "valid_at": None,
        "observed_at": None,
        "evidence": [_locator(s) for s in source_ids] if evidence else [],
        "change_reason": None,
        "provenance_type": "material_publication",
        "evidence_role": evidence_role,
    }


def _fact(
    source_ids: tuple[str, ...] = ("mat-a",),
    invalid_at: str | None = None,
    evidence_role: str | None = None,
) -> dict:
    return {
        "subject": "s",
        "predicate": "p",
        "object": "o",
        "valid_at": DT,
        "invalid_at": invalid_at,
        "observed_at": DT,
        "source_ids": list(source_ids),
        "confidence": 0.9,
        "provenance_type": "material_publication",
        "evidence": [_locator(s) for s in source_ids],
        "fact_id": "fact-1",
        "evidence_role": evidence_role,
        "cited_source_ref": None,
    }


def _claim(
    based_on: tuple[str, ...] = ("mat-a",),
    revised_by: str | None = None,
    claim_type: str = "interpretation",
    evidence_role: str | None = None,
) -> dict:
    return {
        "claim_id": "claim-1",
        "actor": "synthetic actor",
        "proposition": "synthetic proposition",
        "stance": "support",
        "stated_at": DT,
        "based_on": list(based_on),
        "revised_by": revised_by,
        "evidence": [_locator(s) for s in based_on],
        "observed_at": DT,
        "provenance_type": "unspecified",
        "confidence": 1.0,
        "claim_type": claim_type,
        "evidence_role": evidence_role,
    }


def _relation(
    relation_type: str = "supersedes",
    source_ids: tuple[str, ...] = ("mat-a",),
) -> dict:
    return {
        "relation_id": "rel-1",
        "relation_type": relation_type,
        "source_ref": "fact-1",
        "target_ref": "fact-0",
        "valid_at": DT,
        "invalid_at": None,
        "observed_at": DT,
        "source_ids": list(source_ids),
        "evidence": [_locator(s) for s in source_ids],
        "confidence": 1.0,
        "provenance_type": "source_explicit",
        "evidence_role": None,
        "cited_source_ref": None,
    }


def _conflict(source_ids: tuple[str, ...] = ("mat-a",)) -> dict:
    return {
        "conflict_id": "conflict-1",
        "subject": "s",
        "predicate": "p",
        "alternatives": ["a", "b"],
        "source_ids": list(source_ids),
        "evidence": [_locator(s) for s in source_ids],
        "valid_at": None,
        "invalid_at": None,
        "observed_at": DT,
        "confidence": 1.0,
        "provenance_type": "reported_conflict",
        "evidence_role": None,
        "cited_source_ref": None,
    }


def _gap(gap_type: str = "candidate_validation_failed", detail: str = "synthetic gap detail") -> dict:
    return {
        "gap_type": gap_type,
        "detail": detail,
        "item_kind": "node",
        "item_id": "node-x",
        "source_ids": ["mat-a"],
        "candidate_payload": None,
    }


def _extraction(
    nodes: list[dict] | None = None,
    facts: list[dict] | None = None,
    claims: list[dict] | None = None,
    relations: list[dict] | None = None,
    conflicts: list[dict] | None = None,
    gaps: list[dict] | None = None,
) -> dict:
    return {
        "case": None,
        "nodes": nodes or [],
        "temporal_facts": facts or [],
        "claims": claims or [],
        "warnings": [],
        "conflicts": conflicts or [],
        "evidence_gaps": gaps or [],
        "relations": relations or [],
        "evidence_matches": [],
        "material_role": "primary_study",
        "accumulation_status": "case_bound",
    }


# ------------------------------------------------------------------ run fixture


def _build_run_dir(
    root: Path,
    *,
    rows: list[tuple[str, str, dict]],
    outcomes: list[tuple[str, str, str | None, str | None, str | None]] | None = None,
    documents: tuple[str, ...] = ("mat-a", "mat-b"),
    summary: dict | None = None,
    material_rows: list[tuple[str, dict]] | None = None,
) -> Path:
    run_dir = root / "rc-run"
    run_dir.mkdir()
    conn = sqlite3.connect(run_dir / "index.db")
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO documents (source_id) VALUES (?)",
        [(doc,) for doc in documents],
    )
    conn.executemany(
        "INSERT INTO case_extraction_ledger (case_id, material_id,"
        " material_json, extraction_json, recorded_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (case_id, material_id, json.dumps({"id": material_id}),
             json.dumps(extraction), DT, DT)
            for case_id, material_id, extraction in rows
        ],
    )
    conn.executemany(
        "INSERT INTO material_evidence_ledger (material_id, status,"
        " material_json, extraction_json, recorded_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (material_id, "awaiting_case_binding",
             json.dumps({"id": material_id}), json.dumps(extraction), DT, DT)
            for material_id, extraction in (material_rows or [])
        ],
    )
    conn.executemany(
        "INSERT INTO pipeline_outcomes (material_id, status, stage,"
        " error_type, message, occurred_at, correlation_id, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (material_id, status, stage, error_type, message, DT, None, DT)
            for material_id, status, stage, error_type, message in (
                outcomes
                or [("mat-a", "committed", None, None, None)]
            )
        ],
    )
    conn.commit()
    conn.close()
    payload = (
        summary
        if summary is not None
        else {
            "case_id": CASE,
            "input_files": 3,
            "materials": {"successful": 2, "failed": 1},
        }
    )
    (run_dir / "run-summary.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return run_dir


def _read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------ tests


def test_report_metrics_end_to_end(tmp_path: Path) -> None:
    row_a = _extraction(
        nodes=[
            _node("publication", evidence_role="primary_observation"),
            _node("revision"),
        ],
        facts=[_fact(invalid_at=DT2, evidence_role="cited_prior_research")],
        claims=[_claim(revised_by="claim-2", evidence_role="current_synthesis")],
        relations=[_relation("supersedes")],
        conflicts=[_conflict()],
        gaps=[
            _gap("candidate_validation_failed"),
            _gap("evidence_location_failed"),
        ],
    )
    row_b = _extraction(
        nodes=[_node("publication", source_ids=("mat-c",), evidence=False)],
        gaps=[_gap("document_level_gap")],
    )
    awaiting = _extraction(nodes=[_node("publication")])
    run_dir = _build_run_dir(
        tmp_path,
        rows=[(CASE, "mat-a", row_a), (CASE, "mat-b", row_b)],
        outcomes=[
            ("mat-a", "committed", None, None, None),
            ("mat-b", "committed", None, None, None),
            ("mat-c", "failed", "extraction", "ExtractionError", "synthetic failure"),
        ],
        documents=("mat-a", "mat-b"),
        material_rows=[("mat-d", awaiting)],
    )
    output = tmp_path / "quality-report.json"
    rc = gate.main(
        [
            "--run-dir", str(run_dir),
            "--case-id", CASE,
            "--output", str(output),
        ]
    )
    assert rc == 0
    report = _read_report(output)

    assert report["materials"] == {
        "input_files": 3,
        "successful": 2,
        "failed": 1,
        "counts_source": "pipeline_outcomes",
        "failed_by_stage": {"extraction": 1},
    }
    assert report["cases"]["target_case_ids"] == [CASE]
    assert report["cases"]["ledger_rows"] == 2
    assert report["cases"]["distinct_materials"] == 2
    assert report["cases"]["unreadable_rows"] == 0
    assert report["substantive"] == {
        "nodes": 3,
        "temporal_facts": 1,
        "claims": 1,
        "conflicts": 1,
        "relations": 1,
        "total": 7,
    }
    assert report["distributions"]["node_type"] == {
        "publication": 2,
        "revision": 1,
    }
    assert report["distributions"]["claim_type"] == {"interpretation": 1}
    assert report["distributions"]["evidence_role"]["node"] == {
        "primary_observation": 1,
        "unset": 2,
    }
    assert report["distributions"]["evidence_role"]["temporal_fact"] == {
        "cited_prior_research": 1,
    }
    assert report["coverage"]["source_ids"] == {
        "numerator": 7,
        "denominator": 7,
        "rate": 1.0,
        "definition": report["coverage"]["source_ids"]["definition"],
    }
    assert report["coverage"]["evidence_locator"]["numerator"] == 6
    assert report["coverage"]["evidence_locator"]["denominator"] == 7
    assert report["coverage"]["evidence_locator"]["rate"] == 0.8571
    assert report["coverage"]["cited_source_ids_resolved"] == {
        "numerator": 1,
        "denominator": 2,
        "rate": 0.5,
        "definition": report["coverage"]["cited_source_ids_resolved"]["definition"],
    }
    assert report["evidence_gaps"] == {
        "candidate_validation_failed": 1,
        "evidence_location_failed": 1,
        "other": 1,
        "other_types": {"document_level_gap": 1},
        "total": 3,
    }
    assert report["evolution"] == {
        "facts_invalidated": 1,
        "claims_revised": 1,
        "relations_supersedes": 1,
        "relations_revises": 0,
        "relations_contradicts": 0,
        "relations_triggered_by": 0,
    }
    assert report["awaiting_case_binding"] == {
        "rows": 1,
        "substantive_items": 1,
        "evidence_gaps": 0,
    }
    assert report["verdict"]["mechanism_status"] == "partial"
    assert report["verdict"]["semantic_status"] == "partial"
    assert report["verdict"]["reasons"]

    text = output.read_text(encoding="utf-8")
    assert "synthetic node summary" not in text
    assert "synthetic failure" not in text


def test_clean_run_verdict_pass_and_strict_zero(tmp_path: Path) -> None:
    run_dir = _build_run_dir(
        tmp_path,
        rows=[(CASE, "mat-a", _extraction(nodes=[_node(evidence_role="primary_observation")]))],
        outcomes=[("mat-a", "committed", None, None, None)],
        documents=("mat-a",),
        summary={
            "case_id": CASE,
            "input_files": 1,
            "materials": {"successful": 1, "failed": 0},
        },
    )
    output = tmp_path / "quality-report.json"
    rc = gate.main(
        ["--run-dir", str(run_dir), "--output", str(output), "--strict"]
    )
    assert rc == 0
    report = _read_report(output)
    assert report["verdict"] == {
        "mechanism_status": "pass",
        "semantic_status": "pass",
        "reasons": [],
    }


def test_pipeline_success_alone_is_not_semantic_pass(tmp_path: Path) -> None:
    rows = [
        (CASE, "mat-a", _extraction(gaps=[_gap("candidate_validation_failed")])),
        (CASE, "mat-b", _extraction()),
    ]
    run_dir = _build_run_dir(
        tmp_path,
        rows=rows,
        outcomes=[
            ("mat-a", "committed", None, None, None),
            ("mat-b", "committed", None, None, None),
        ],
        documents=("mat-a", "mat-b"),
        summary={
            "case_id": CASE,
            "input_files": 2,
            "materials": {"successful": 2, "failed": 0},
        },
    )
    output = tmp_path / "quality-report.json"
    rc = gate.main(["--run-dir", str(run_dir), "--output", str(output)])
    assert rc == 0
    report = _read_report(output)
    assert report["materials"]["successful"] == 2
    assert report["materials"]["failed"] == 0
    assert report["verdict"]["mechanism_status"] == "pass"
    assert report["verdict"]["semantic_status"] == "fail"
    assert any(
        "no substantive extracted candidates" in reason
        for reason in report["verdict"]["reasons"]
    )


def test_report_scrubs_paths_secrets_and_content(tmp_path: Path) -> None:
    leaked = str(tmp_path)
    evil_node = {
        "id": "node-evil",
        "case_id": CASE,
        "node_type": "C:\\evil\\path",
        "happened_at": DT,
        "summary": f"leaked {leaked} D:\\Hermas\\workspace api_key=sk-123",
        "source_ids": ["mat-a"],
        "claim_ids": [],
        "valid_at": None,
        "observed_at": None,
        "evidence": [],
        "change_reason": None,
        "provenance_type": None,
        "evidence_role": None,
    }
    run_dir = _build_run_dir(
        tmp_path,
        rows=[(CASE, "mat-a", _extraction(nodes=[evil_node]))],
        outcomes=[("mat-a", "committed", None, None, None)],
        documents=("mat-a",),
        summary={
            "case_id": CASE,
            "input_files": 1,
            "workspace_root": leaked,
            "materials": {"successful": 1, "failed": 0},
        },
    )
    output = tmp_path / "quality-report.json"
    rc = gate.main(["--run-dir", str(run_dir), "--output", str(output)])
    assert rc == 0
    text = output.read_text(encoding="utf-8")

    assert leaked not in text
    assert "Hermas" not in text
    assert "sk-123" not in text
    assert "api_key" not in text
    assert "leaked" not in text
    assert "synthetic locator quote" not in text
    assert re.search(r"[A-Za-z]:[/\\]", text) is None
    report = json.loads(text)
    assert report["distributions"]["node_type"] == {"other": 1}
    assert report["verdict"]["semantic_status"] == "partial"


def test_run_dir_discovery_missing_case_and_strict(tmp_path: Path) -> None:
    run_dir = _build_run_dir(
        tmp_path,
        rows=[(CASE, "mat-a", _extraction(nodes=[_node()]))],
        outcomes=[("mat-a", "committed", None, None, None)],
        documents=("mat-a",),
        summary={
            "case_id": CASE,
            "input_files": 1,
            "materials": {"successful": 1, "failed": 0},
        },
    )
    output = tmp_path / "ghost-report.json"
    rc = gate.main(
        [
            "--run-dir", str(run_dir),
            "--case-id", "ghost-case",
            "--output", str(output),
        ]
    )
    assert rc == 0
    report = _read_report(output)
    assert report["cases"]["target_case_ids"] == ["ghost-case"]
    assert report["cases"]["ledger_rows"] == 0
    assert report["verdict"]["mechanism_status"] == "fail"
    assert report["verdict"]["semantic_status"] == "fail"

    rc = gate.main(
        [
            "--run-dir", str(run_dir),
            "--case-id", "ghost-case",
            "--output", str(output),
            "--strict",
        ]
    )
    assert rc == 1

    default_output = tmp_path / "default-case-report.json"
    rc = gate.main(["--run-dir", str(run_dir), "--output", str(default_output)])
    assert rc == 0
    assert _read_report(default_output)["cases"]["target_case_ids"] == [CASE]


def test_input_and_data_errors_classified(tmp_path: Path, capsys) -> None:
    run_dir = _build_run_dir(
        tmp_path, rows=[(CASE, "mat-a", _extraction(nodes=[_node()]))]
    )
    summary = str(run_dir / "run-summary.json")
    index = str(run_dir / "index.db")

    rc = gate.main(
        ["--run-summary", str(tmp_path / "nope.json"), "--index-db", index,
         "--case-id", CASE]
    )
    assert rc == 2
    assert "input-error" in capsys.readouterr().err

    rc = gate.main(
        ["--run-summary", summary, "--index-db", str(tmp_path / "nope.db"),
         "--case-id", CASE]
    )
    assert rc == 2
    assert "input-error" in capsys.readouterr().err

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"definitely not a sqlite database" * 8)
    rc = gate.main(
        ["--run-summary", summary, "--index-db", str(corrupt), "--case-id", CASE]
    )
    assert rc == 3
    assert "data-error" in capsys.readouterr().err

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    rc = gate.main(
        ["--run-summary", summary, "--index-db", str(empty), "--case-id", CASE]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "data-error" in err and "required table" in err

    partial = tmp_path / "partial.db"
    conn = sqlite3.connect(partial)
    conn.execute("CREATE TABLE documents (source_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    rc = gate.main(
        ["--run-summary", summary, "--index-db", str(partial), "--case-id", CASE]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "data-error" in err and "case_extraction_ledger" in err

    rc = gate.main(["--run-summary", summary, "--index-db", index, "--case-id", " "])
    assert rc == 2
    assert "usage-error" in capsys.readouterr().err


def test_report_to_stdout_without_output(tmp_path: Path, capsys) -> None:
    run_dir = _build_run_dir(
        tmp_path,
        rows=[(CASE, "mat-a", _extraction(nodes=[_node()]))],
        outcomes=[("mat-a", "committed", None, None, None)],
        documents=("mat-a",),
        summary={
            "case_id": CASE,
            "input_files": 1,
            "materials": {"successful": 1, "failed": 0},
        },
    )
    rc = gate.main(["--run-dir", str(run_dir)])
    assert rc == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["cases"]["target_case_ids"] == [CASE]
    assert captured.err.startswith("prism-quality-gate:")


def test_real_ledger_codec_payload_is_counted(tmp_path: Path) -> None:
    from prism.cases.ledger import extraction_to_json, material_to_json
    from prism.domain.models import (
        Claim,
        EvidenceLocator,
        EvolutionCase,
        EvolutionNode,
        Material,
        TemporalFact,
    )
    from prism.extraction.service import ExtractionResult

    when = datetime(2026, 1, 1, tzinfo=UTC)
    material = Material(
        id="mat-x",
        title="synthetic title",
        source="synthetic-source",
        published_at=when,
        fetched_at=when,
        type="news",
        content="synthetic body",
    )
    locator = EvidenceLocator(
        source_id="mat-x",
        corpus_path="corpus/mat-x.md",
        paragraph=1,
        quote="synthetic",
    )
    case = EvolutionCase(
        case_id="fidelity-case",
        case_type="policy",
        canonical_name="synthetic",
        start_at=when,
        status="open",
    )
    node = EvolutionNode(
        id="n1",
        case_id="fidelity-case",
        node_type="publication",
        happened_at=when,
        summary="synthetic",
        source_ids=("mat-x",),
        evidence=(locator,),
        evidence_role="primary_observation",
    )
    fact = TemporalFact(
        subject="s",
        predicate="p",
        object="o",
        valid_at=when,
        invalid_at=None,
        observed_at=when,
        source_ids=("mat-x",),
        confidence=0.9,
        provenance_type="material_publication",
        evidence=(locator,),
        evidence_role="cited_prior_research",
    )
    claim = Claim(
        claim_id="c1",
        actor="actor",
        proposition="synthetic",
        stance="support",
        stated_at=when,
        based_on=("mat-x",),
        evidence=(locator,),
        claim_type="value_judgment",
    )
    result = ExtractionResult(
        case=case,
        nodes=(node,),
        temporal_facts=(fact,),
        claims=(claim,),
    )

    run_dir = tmp_path / "rc-run"
    run_dir.mkdir()
    conn = sqlite3.connect(run_dir / "index.db")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO documents (source_id) VALUES ('mat-x')")
    conn.execute(
        "INSERT INTO case_extraction_ledger (case_id, material_id,"
        " material_json, extraction_json, recorded_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("fidelity-case", "mat-x", material_to_json(material),
         extraction_to_json(result), DT, DT),
    )
    conn.execute(
        "INSERT INTO pipeline_outcomes (material_id, status, stage, error_type,"
        " message, occurred_at, correlation_id, updated_at)"
        " VALUES ('mat-x', 'committed', NULL, NULL, NULL, ?, NULL, ?)",
        (DT, DT),
    )
    conn.commit()
    conn.close()
    (run_dir / "run-summary.json").write_text(
        json.dumps(
            {
                "case_id": "fidelity-case",
                "input_files": 1,
                "materials": {"successful": 1, "failed": 0},
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "quality-report.json"
    rc = gate.main(["--run-dir", str(run_dir), "--output", str(output), "--strict"])
    assert rc == 0
    report = _read_report(output)
    assert report["substantive"] == {
        "nodes": 1,
        "temporal_facts": 1,
        "claims": 1,
        "conflicts": 0,
        "relations": 0,
        "total": 3,
    }
    assert report["distributions"]["claim_type"] == {"value_judgment": 1}
    for name in ("source_ids", "evidence_locator", "cited_source_ids_resolved"):
        assert report["coverage"][name]["rate"] == 1.0
    assert report["verdict"]["mechanism_status"] == "pass"
    assert report["verdict"]["semantic_status"] == "pass"
