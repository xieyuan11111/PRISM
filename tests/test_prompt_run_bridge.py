"""TDD tests for the live runner's sanitized prompt-run-summary bridge.

The bridge projects THIS run's own SQLite ``case_extraction_ledger`` rows
plus the quality-gate results into one ``prompt-run-summary.json`` that
``tools/prism_prompt_benchmark.py`` can read directly.  The projection is
ids, closed-vocabulary types, gap-type counts, coverage rates and verdict
statuses — never material content, quotes, candidate payloads, corpus or
absolute paths, secrets or prompt text.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import run_live_case_acceptance as runner  # noqa: E402
import prism_prompt_benchmark as benchmark  # noqa: E402


CASE_ID = runner.CASE_ID

# Distinctive markers planted in the synthetic ledger payloads.  If any of
# them can reach the bridge, the sanitization red tests below fail.
LEAK_MARKERS = (
    "SECRET-QUOTE-kJ3",
    "SECRET-SUMMARY-zqX9",
    "SECRET-PAYLOAD-mQ7",
    "E:\\private\\corpus\\material.md",
    "/home/xieyu/corpus/material.md",
)


def _node(node_id, node_type, **extra):
    return {
        "id": node_id,
        "case_id": CASE_ID,
        "node_type": node_type,
        "happened_at": "2026-01-10T00:00:00+00:00",
        "summary": f"summary {node_id} SECRET-SUMMARY-zqX9",
        "source_ids": ["mat_source"],
        "evidence": [
            {
                "source_id": "mat_source",
                "quote": "verbatim SECRET-QUOTE-kJ3",
                "paragraph": 1,
                "page": None,
            }
        ],
        **extra,
    }


def synthetic_extraction_a() -> dict:
    return {
        "case": {
            "case_id": CASE_ID,
            "case_type": "policy",
            "canonical_name": "Synthetic case",
            "start_at": "2026-01-10T00:00:00+00:00",
            "status": "active",
        },
        "nodes": [
            _node("policy-2026-proposal", "proposal"),
            # Unsafe ids (paths, prose, over-long) must never be emitted.
            _node("E:\\private\\北京 policy", "proposal"),
            _node("a" * 65, "proposal"),
            _node("node-untyped", "行业类型/sector"),
        ],
        "temporal_facts": [
            {
                "subject": "loan floor",
                "predicate": "set to",
                "object": "15%",
                "fact_id": "fact-rate-adjustment",
                "provenance_type": "source_explicit",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "observed_at": "2026-02-15T00:00:00+00:00",
                "evidence": [],
            },
            {
                "subject": "rate",
                "predicate": "cut by",
                "object": "0.25pp",
                "fact_id": None,
                "provenance_type": "source_explicit",
                "evidence": [],
            },
        ],
        "claims": [
            {
                "claim_id": "claim-forecast-q3",
                "claim_type": "prediction",
                "actor": "analyst",
                "proposition": "SECRET-SUMMARY-zqX9 forecast",
                "stance": "uncertain",
                "stated_at": "2026-02-15T00:00:00+00:00",
            }
        ],
        "conflicts": [
            {
                "conflict_id": "conflict-loan-floor",
                "provenance_type": "reported_conflict",
                "subject": "loan floor",
                "predicate": "set to",
                "alternatives": ["15%", "10%"],
            }
        ],
        "relations": [
            {
                "relation_id": "rel-supersedes-1",
                "relation_type": "supersedes",
                "source_ref": "policy-2026-proposal",
                "target_ref": "policy-2026-implementation",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "observed_at": "2026-02-15T00:00:00+00:00",
            }
        ],
        "evidence_gaps": [
            {
                "gap_type": "candidate_validation_failed",
                "detail": "candidate 2 was not graph-ready SECRET-SUMMARY-zqX9",
                "item_kind": "node",
                "item_id": "proposal-x",
                "source_ids": ["mat_source"],
                "candidate_payload": {
                    "corpus_path": "E:\\private\\corpus\\material.md",
                    "quote": "SECRET-PAYLOAD-mQ7",
                    "summary": "SECRET-SUMMARY-zqX9",
                },
            },
            {
                "gap_type": "evidence_location_failed",
                "detail": "quote not found SECRET-QUOTE-kJ3",
                "item_kind": "temporal_fact",
                "item_id": None,
                "source_ids": ["mat_source"],
            },
        ],
        "warnings": [],
    }


def synthetic_extraction_b() -> dict:
    return {
        "case": synthetic_extraction_a()["case"],
        "nodes": [
            _node("policy-2026-proposal", "proposal"),
            _node("policy-2026-implementation", "implementation"),
        ],
        "temporal_facts": [],
        "claims": [],
        "conflicts": [],
        "relations": [],
        "evidence_gaps": [
            {
                "gap_type": "candidate_validation_failed",
                "detail": "detail",
                "item_kind": "node",
                "item_id": None,
                "source_ids": ["mat_source"],
            }
        ],
        "warnings": [],
    }


def synthetic_quality() -> dict:
    return {
        "verdict": {
            "mechanism_status": "pass",
            "semantic_status": "partial",
            "reasons": ["synthetic reason"],
        },
        "coverage": {
            "source_ids": {
                "rate": 1.0,
                "numerator": 4,
                "denominator": 4,
                "description": "substantive items citing at least one source id",
            },
            "evidence_locator": {"rate": 0.75, "numerator": 3, "denominator": 4},
            "cited_source_ids_resolved": {"rate": None, "numerator": 0, "denominator": 0},
        },
    }


def expected_candidates() -> dict:
    return {
        "node": {
            "ids": [
                "node-untyped",
                "policy-2026-implementation",
                "policy-2026-proposal",
            ],
            "types": {
                "proposal": 4,
                "implementation": 1,
                "other": 1,
            },
        },
        "temporal_fact": {
            "ids": ["fact-rate-adjustment"],
            "types": {"source_explicit": 2},
        },
        "claim": {"ids": ["claim-forecast-q3"], "types": {"prediction": 1}},
        "conflict": {"ids": ["conflict-loan-floor"], "types": {"reported_conflict": 1}},
        "relation": {"ids": ["rel-supersedes-1"], "types": {"supersedes": 1}},
    }


def test_bridge_projection_is_ids_types_gaps_coverage_verdict_only() -> None:
    summary = runner.build_prompt_run_summary(
        profile="protocol-v1",
        run_id="run-01",
        case_id=CASE_ID,
        extractions=(synthetic_extraction_a(), synthetic_extraction_b()),
        quality=synthetic_quality(),
    )

    assert summary == {
        "schema_version": runner.BRIDGE_SCHEMA_VERSION,
        "tool": runner.TOOL_NAME,
        "profile": "protocol-v1",
        "run_id": "run-01",
        "case_id": CASE_ID,
        "candidates": expected_candidates(),
        "gap_types": {
            "candidate_validation_failed": 2,
            "evidence_location_failed": 1,
        },
        "coverage": {"source_ids": 1.0, "evidence_locator": 0.75},
        "verdict": {"mechanism_status": "pass", "semantic_status": "partial"},
    }


def test_bridge_projection_never_leaks_material_paths_quotes_or_payloads() -> None:
    summary = runner.build_prompt_run_summary(
        profile="baseline",
        run_id="run-01",
        case_id=CASE_ID,
        extractions=(synthetic_extraction_a(),),
        quality=synthetic_quality(),
    )
    rendered = json.dumps(summary, sort_keys=True)
    for marker in LEAK_MARKERS:
        assert marker not in rendered, marker
    for forbidden_key in ("quote", "summary", "payload", "corpus_path", "path"):
        assert f'"{forbidden_key}"' not in rendered
    # The output sanitizer must accept the finished bridge.
    runner.guard_public_summary(summary)


def test_bridge_verdict_defaults_to_fail_when_quality_is_missing() -> None:
    summary = runner.build_prompt_run_summary(
        profile="baseline",
        run_id="run-01",
        case_id=CASE_ID,
        extractions=(),
        quality={},
    )
    assert summary["verdict"] == {
        "mechanism_status": "fail",
        "semantic_status": "fail",
    }
    assert summary["candidates"] == {
        kind: {"ids": [], "types": {}} for kind in expected_candidates()
    }
    assert summary["gap_types"] == {}
    assert summary["coverage"] == {}


def _ledger_row(case_id: str, material_id: str, extraction: dict, recorded: str):
    return (
        case_id,
        material_id,
        json.dumps({"id": material_id, "content": "SECRET-SUMMARY-zqX9"}),
        json.dumps(extraction),
        recorded,
        recorded,
    )


def plant_ledger(home: Path) -> None:
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(data / "index.db"))
    connection.executescript(
        "CREATE TABLE IF NOT EXISTS case_extraction_ledger ("
        "case_id TEXT NOT NULL, material_id TEXT NOT NULL,"
        "material_json TEXT NOT NULL, extraction_json TEXT NOT NULL,"
        "recorded_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        "PRIMARY KEY (case_id, material_id));"
    )
    connection.executemany(
        "INSERT INTO case_extraction_ledger VALUES (?,?,?,?,?,?)",
        (
            _ledger_row(
                CASE_ID, "mat_b", synthetic_extraction_b(), "2026-09-05T00:00:01+00:00"
            ),
            _ledger_row(
                CASE_ID, "mat_a", synthetic_extraction_a(), "2026-09-05T00:00:00+00:00"
            ),
            # Rows of another case (and any old run) must never be projected.
            _ledger_row(
                "other-case-legacy",
                "mat_z",
                synthetic_extraction_a(),
                "2026-09-05T00:00:02+00:00",
            ),
        ),
    )
    connection.commit()
    connection.close()


def test_bridge_reads_only_this_runs_own_case_rows_in_recorded_order(
    tmp_path: Path,
) -> None:
    home = tmp_path / "prism-home"
    plant_ledger(home)

    extractions = runner.read_case_extractions(home, CASE_ID)

    # Recorded order (mat_a with 4 nodes first, mat_b with 2), never the
    # other case's row; material bodies from material_json never surface.
    assert [len(item["nodes"]) for item in extractions] == [4, 2]
    assert all(item["case"]["case_id"] == CASE_ID for item in extractions)


def test_bridge_fails_closed_when_the_run_home_has_no_ledger(tmp_path: Path) -> None:
    home = tmp_path / "prism-home"
    home.mkdir(parents=True)
    with pytest.raises(runner.AcceptanceRuntimeError, match="index.db"):
        runner.read_case_extractions(home, CASE_ID)


def test_bridge_fails_closed_on_unreadable_ledger_rows(tmp_path: Path) -> None:
    home = tmp_path / "prism-home"
    data = home / "data"
    data.mkdir(parents=True)
    connection = sqlite3.connect(str(data / "index.db"))
    connection.executescript(
        "CREATE TABLE case_extraction_ledger ("
        "case_id TEXT, material_id TEXT, material_json TEXT,"
        "extraction_json TEXT, recorded_at TEXT, updated_at TEXT)"
    )
    connection.execute(
        "INSERT INTO case_extraction_ledger VALUES (?,?,?,?,?,?)",
        (CASE_ID, "mat_bad", "{}", "not-json{", "t", "t"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(runner.AcceptanceRuntimeError):
        runner.read_case_extractions(home, CASE_ID)


def _write_bridge(path: Path, payload: dict) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def test_benchmark_reads_bridge_files_directly_and_pair_runs_yield_stability(
    tmp_path: Path,
) -> None:
    first = runner.build_prompt_run_summary(
        profile="protocol-v1",
        run_id="run-01",
        case_id=CASE_ID,
        extractions=(synthetic_extraction_a(), synthetic_extraction_b()),
        quality=synthetic_quality(),
    )
    repeat = runner.build_prompt_run_summary(
        profile="protocol-v1",
        run_id="run-02",
        case_id=CASE_ID,
        extractions=(
            synthetic_extraction_a(),
            synthetic_extraction_b(),
        ),
        quality=synthetic_quality(),
    )
    files = [
        _write_bridge(tmp_path / "prompt-run-summary-1.json", first),
        _write_bridge(tmp_path / "prompt-run-summary-2.json", repeat),
    ]

    report = benchmark.build_report(files)

    assert report["verdict"]["stability_status"] == "stable"
    assert report["inputs"]["run_summaries"] == 2
    assert report["inputs"]["profiles"] == ["protocol-v1"]
    group = report["profiles"]["protocol-v1"]["cases"][CASE_ID]
    assert group["runs"] == 2
    assert group["stability"]["status"] == "stable"
    assert group["candidates"]["node"]["ids"]["union"] == sorted(
        expected_candidates()["node"]["ids"]
    )
    assert group["mechanism"]["status_counts"] == {"pass": 2}
    assert group["semantic"]["status_counts"] == {"partial": 2}


def test_benchmark_still_rejects_leaky_bridge_payloads(tmp_path: Path) -> None:
    leaky = runner.build_prompt_run_summary(
        profile="protocol-v1",
        run_id="run-01",
        case_id=CASE_ID,
        extractions=(),
        quality={},
    )
    leaky["quote"] = "SECRET-QUOTE-kJ3"
    path = _write_bridge(tmp_path / "prompt-run-summary.json", leaky)

    with pytest.raises(benchmark.BenchmarkError):
        benchmark.build_report([path])
