"""Tests for the offline PRISM prompt-profile benchmark tool.

Everything runs against synthetic, already-sanitized per-run summary JSON
in a temporary directory: no network, no LLM, no real provider call, no
material body text.  The tool only measures cross-run agreement; node
counts are never a success criterion and missing relations never fail a
profile because no case-specific expected relations are assumed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import prism_prompt_benchmark as bench  # noqa: E402

CANDIDATE_KINDS = ("node", "temporal_fact", "claim", "conflict", "relation")


def run_summary(
    run_id,
    profile="protocol-v1",
    case_id="case-alpha",
    *,
    nodes=(),
    node_types=(),
    relations=(),
    relation_types=(),
    facts=(),
    gap_types=None,
    coverage=None,
    mechanism="pass",
    semantic="partial",
):
    candidates = {
        "node": {"ids": sorted(nodes), "types": dict(node_types)},
        "temporal_fact": {"ids": sorted(facts), "types": {}},
        "claim": {"ids": [], "types": {}},
        "conflict": {"ids": [], "types": {}},
        "relation": {"ids": sorted(relations), "types": dict(relation_types)},
    }
    summary = {
        "schema_version": 1,
        "profile": profile,
        "run_id": run_id,
        "case_id": case_id,
        "candidates": candidates,
    }
    if gap_types is not None:
        summary["gap_types"] = gap_types
    if coverage is not None:
        summary["coverage"] = coverage
    if mechanism is not None or semantic is not None:
        summary["verdict"] = {
            "mechanism_status": mechanism,
            "semantic_status": semantic,
        }
    return summary


def write_runs(tmp_path, runs, subdirectory=False):
    target = tmp_path / "runs" if subdirectory else tmp_path
    target.mkdir(parents=True, exist_ok=True)
    for run in runs:
        (target / f"{run['run_id']}.json").write_text(
            json.dumps(run), encoding="utf-8"
        )
    return target


def build(tmp_path, runs, subdirectory=False):
    target = write_runs(tmp_path, runs, subdirectory=subdirectory)
    return bench.build_report([target])


# ---------------------------------------------------------------- reducer core


def test_stability_reducer_computes_intersection_union_frequency(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", nodes=["node-a", "node-b"]),
            run_summary("run-2", nodes=["node-b", "node-a"]),
            run_summary("run-3", nodes=["node-b", "node-c"]),
        ],
    )

    group = report["profiles"]["protocol-v1"]["cases"]["case-alpha"]
    node = group["candidates"]["node"]
    assert group["runs"] == 3
    assert node["per_run_ids"] == [
        ["node-a", "node-b"],
        ["node-a", "node-b"],
        ["node-b", "node-c"],
    ]
    assert node["ids"]["intersection"] == ["node-b"]
    assert node["ids"]["union"] == ["node-a", "node-b", "node-c"]
    assert node["ids"]["frequency"] == {"node-a": 2, "node-b": 3, "node-c": 1}
    assert node["ids"]["stability_rate"] == round(1 / 3, 4)
    assert node["ids"]["status"] == "unstable"
    assert node["ids"]["differing_ids"] == ["node-a", "node-c"]


def test_perfectly_stable_group_is_stable(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", nodes=["node-a"]),
            run_summary("run-2", nodes=["node-a"]),
        ],
    )

    node = report["profiles"]["protocol-v1"]["cases"]["case-alpha"]["candidates"]["node"]
    assert node["ids"]["status"] == "stable"
    assert node["ids"]["stability_rate"] == 1.0
    assert report["verdict"]["stability_status"] == "stable"
    assert report["verdict"]["reasons"] == []


def test_candidate_type_labels_are_tracked_across_runs(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", node_types={"proposal": 1, "revision": 2}),
            run_summary("run-2", node_types={"proposal": 2}),
        ],
    )

    types = report["profiles"]["protocol-v1"]["cases"]["case-alpha"]["candidates"][
        "node"
    ]["types"]
    assert types["per_run"] == [{"proposal": 1, "revision": 2}, {"proposal": 2}]
    assert types["union"] == ["proposal", "revision"]
    assert types["intersection"] == ["proposal"]
    assert types["frequency"] == {"proposal": 2, "revision": 1}


def test_relation_ids_and_types_are_tracked(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary(
                "run-1",
                nodes=["node-a", "node-b"],
                relations=["rel-1"],
                relation_types={"revises": 1},
            ),
            run_summary(
                "run-2",
                nodes=["node-a", "node-b"],
                relations=["rel-1"],
                relation_types={"revises": 1},
            ),
        ],
    )

    candidates = report["profiles"]["protocol-v1"]["cases"]["case-alpha"]["candidates"]
    assert candidates["relation"]["ids"]["union"] == ["rel-1"]
    assert candidates["relation"]["ids"]["status"] == "stable"
    assert candidates["relation"]["types"]["union"] == ["revises"]


def test_gap_types_and_coverage_are_aggregated(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary(
                "run-1",
                gap_types={"candidate_validation_failed": 2},
                coverage={"source_ids": 1.0, "evidence_locator": 0.5},
            ),
            run_summary(
                "run-2",
                gap_types={"candidate_validation_failed": 4, "review_context": 1},
                coverage={"source_ids": 0.9, "evidence_locator": 0.5},
            ),
        ],
    )

    group = report["profiles"]["protocol-v1"]["cases"]["case-alpha"]
    assert group["gap_types"]["per_run"] == [
        {"candidate_validation_failed": 2},
        {"candidate_validation_failed": 4, "review_context": 1},
    ]
    assert group["gap_types"]["frequency"] == {
        "candidate_validation_failed": 2,
        "review_context": 1,
    }
    assert group["coverage"]["source_ids"] == {"min": 0.9, "max": 1.0}
    assert group["coverage"]["evidence_locator"] == {"min": 0.5, "max": 0.5}


# ------------------------------------------------- mechanism vs semantic split


def test_mechanism_and_semantic_are_explicit_separate_sections(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", mechanism="pass", semantic="pass"),
            run_summary("run-2", mechanism="pass", semantic="partial"),
            run_summary("run-3", mechanism="fail", semantic="fail"),
        ],
    )

    profile = report["profiles"]["protocol-v1"]
    assert profile["mechanism"] == {
        "status_counts": {"pass": 2, "fail": 1},
        "runs_with_verdict": 3,
    }
    assert profile["semantic"] == {
        "status_counts": {"pass": 1, "partial": 1, "fail": 1},
        "runs_with_verdict": 3,
    }
    group = profile["cases"]["case-alpha"]
    assert group["mechanism"] == {"status_counts": {"pass": 2, "fail": 1}}
    assert group["semantic"] == {"status_counts": {"pass": 1, "partial": 1, "fail": 1}}


def test_runs_without_verdict_are_counted_separately(tmp_path):
    report = build(tmp_path, [run_summary("run-1", mechanism=None, semantic=None)])

    profile = report["profiles"]["protocol-v1"]
    assert profile["mechanism"]["runs_with_verdict"] == 0
    assert profile["mechanism"]["status_counts"] == {}
    assert profile["runs_without_verdict"] == 1


# ------------------------------------------------------- verdict policy rules


def test_node_counts_are_never_a_success_criterion(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", nodes=[]),
            run_summary("run-2", nodes=[]),
            run_summary("run-3", nodes=[]),
        ],
    )

    node = report["profiles"]["protocol-v1"]["cases"]["case-alpha"]["candidates"]["node"]
    assert node["ids"]["union"] == []
    assert node["ids"]["stability_rate"] is None
    assert node["ids"]["status"] == "not_applicable"
    assert report["verdict"]["stability_status"] == "stable"
    assert report["policy"]["node_counts_are_not_a_success_criterion"] is True


def test_zero_node_profile_does_not_fail_against_node_heavy_profile(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", profile="protocol-v1", nodes=[]),
            run_summary("run-2", profile="protocol-v1", nodes=[]),
            run_summary(
                "run-3",
                profile="baseline",
                nodes=["node-a", "node-b", "node-c", "node-d"],
            ),
            run_summary(
                "run-4",
                profile="baseline",
                nodes=["node-a", "node-b", "node-c", "node-d"],
            ),
        ],
    )

    # Cross-profile comparison of node counts is explicitly not a verdict
    # input: both profiles are judged only by their own cross-run agreement.
    assert report["profiles"]["protocol-v1"]["cases"]["case-alpha"]["runs"] == 2
    assert report["profiles"]["baseline"]["cases"]["case-alpha"]["runs"] == 2
    assert report["verdict"]["stability_status"] == "stable"


def test_missing_relations_do_not_fail_without_expected_relations(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", nodes=["node-a"], relations=[]),
            run_summary("run-2", nodes=["node-a"], relations=[]),
        ],
    )

    relation = report["profiles"]["protocol-v1"]["cases"]["case-alpha"]["candidates"][
        "relation"
    ]
    assert relation["ids"]["union"] == []
    assert relation["ids"]["status"] == "not_applicable"
    assert report["verdict"]["stability_status"] == "stable"
    assert (
        report["policy"]["missing_relations_do_not_fail"]
        == "no case-specific expected relations are assumed; an empty "
        "relation union is not applicable, never a failure"
    )


def test_single_run_group_is_flagged_insufficient_not_stable(tmp_path):
    report = build(tmp_path, [run_summary("run-1", nodes=["node-a"])])

    group = report["profiles"]["protocol-v1"]["cases"]["case-alpha"]
    assert group["stability"]["status"] == "insufficient_runs"
    assert report["verdict"]["stability_status"] == "insufficient_runs"


def test_unstable_kind_drives_verdict_with_reasons(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", nodes=["node-a"]),
            run_summary("run-2", nodes=["node-b"]),
        ],
    )

    assert report["verdict"]["stability_status"] == "unstable"
    assert any(
        "protocol-v1" in reason and "case-alpha" in reason and "node" in reason
        for reason in report["verdict"]["reasons"]
    )


def test_profiles_and_cases_are_isolated_groups(tmp_path):
    report = build(
        tmp_path,
        [
            run_summary("run-1", profile="baseline", case_id="case-alpha"),
            run_summary("run-2", profile="baseline", case_id="case-beta"),
            run_summary("run-3", profile="protocol-v1", case_id="case-alpha"),
        ],
    )

    assert set(report["profiles"]) == {"baseline", "protocol-v1"}
    assert set(report["profiles"]["baseline"]["cases"]) == {
        "case-alpha",
        "case-beta",
    }
    assert report["inputs"]["profiles"] == ["baseline", "protocol-v1"]


def test_policy_states_offline_only_and_no_live_provider(tmp_path):
    report = build(tmp_path, [run_summary("run-1"), run_summary("run-2")])

    assert (
        report["policy"]["live_provider_execution"]
        == "not implemented; this tool reads offline sanitized summaries only "
        "and never calls an LLM provider"
    )


# ------------------------------------------------------------- input contract


def test_duplicate_run_id_within_group_is_rejected(tmp_path):
    target = tmp_path
    (target / "summary-a.json").write_text(
        json.dumps(run_summary("run-1", nodes=["node-a"])), encoding="utf-8"
    )
    (target / "summary-b.json").write_text(
        json.dumps(run_summary("run-1", nodes=["node-b"])), encoding="utf-8"
    )
    with pytest.raises(bench.BenchmarkError) as excinfo:
        bench.build_report([target])
    assert excinfo.value.kind == "data"
    assert "duplicate run_id" in excinfo.value.message


@pytest.mark.parametrize(
    "bad",
    [
        {"quote": "verbatim material fragment"},
        {"candidates": {"node": {"ids": ["n"], "types": {}, "text": "body"}}},
        {"body": "whole material body"},
        {"prompt": "the prompt text"},
    ],
)
def test_summaries_carrying_material_like_fields_are_rejected(tmp_path, bad):
    summary = run_summary("run-1")
    summary.update(bad)
    target = write_runs(tmp_path, [summary])
    with pytest.raises(bench.BenchmarkError, match="material"):
        bench.build_report([target])


def test_long_prose_values_are_rejected(tmp_path):
    summary = run_summary("run-1")
    summary["case_id"] = "c" * 300
    target = write_runs(tmp_path, [summary])
    with pytest.raises(bench.BenchmarkError):
        bench.build_report([target])


def test_unknown_top_level_field_is_rejected(tmp_path):
    summary = run_summary("run-1")
    summary["notes"] = "free text"
    target = write_runs(tmp_path, [summary])
    with pytest.raises(bench.BenchmarkError, match="unexpected field"):
        bench.build_report([target])


def test_unknown_candidate_kind_and_bad_status_are_rejected(tmp_path):
    summary = run_summary("run-1")
    summary["candidates"]["monograph"] = {"ids": [], "types": {}}
    target = write_runs(tmp_path, [summary])
    with pytest.raises(bench.BenchmarkError):
        bench.build_report([target])

    summary = run_summary("run-2")
    summary["verdict"]["mechanism_status"] = "excellent"
    target = write_runs(tmp_path, [summary])
    with pytest.raises(bench.BenchmarkError):
        bench.build_report([target])


def test_missing_required_fields_rejected(tmp_path):
    target = write_runs(tmp_path, [run_summary("run-1")])
    payload = json.loads((target / "run-1.json").read_text(encoding="utf-8"))
    del payload["candidates"]
    (target / "run-1.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(bench.BenchmarkError):
        bench.build_report([target])


def test_missing_input_and_invalid_json_are_input_errors(tmp_path):
    with pytest.raises(bench.BenchmarkError) as excinfo:
        bench.build_report([tmp_path / "does-not-exist"])
    assert excinfo.value.kind == "input"

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(bench.BenchmarkError) as excinfo:
        bench.build_report([bad])
    assert excinfo.value.kind == "data"


def test_empty_directory_is_a_data_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(bench.BenchmarkError):
        bench.build_report([empty])


# ------------------------------------------------------------------ sanitizer


def test_report_has_no_input_paths_secrets_or_long_strings(tmp_path):
    secret_summary = run_summary("run-1")
    secret_summary["profile"] = "baseline"
    target = write_runs(tmp_path, [secret_summary, run_summary("run-2")])
    forbidden = {str(target), str(target.resolve())}

    report = bench.build_report([target])

    rendered = json.dumps(report)
    assert str(tmp_path) not in rendered
    for fragment in forbidden:
        assert fragment not in rendered
    assert "api" not in rendered.lower() or "api_key" not in rendered.lower()

    def visit(node):
        if isinstance(node, dict):
            for key, value in node.items():
                visit(key)
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, str):
            assert len(node) <= 200

    visit(report)


def test_sanitizer_rejects_leaky_report_before_it_leaves(tmp_path):
    leaky = {
        "profiles": {
            "protocol-v1": {"cases": {"case-alpha": {"runs": 1, "candidates": {}}}}
        },
        "leak": f"material stored at {tmp_path}\\materials\\case-alpha.md",
    }
    with pytest.raises(bench.BenchmarkError) as excinfo:
        bench._guard_sanitized(leaky, set())
    assert excinfo.value.kind == "sanitization"


# ----------------------------------------------------------------------- CLI


def test_cli_writes_sanitized_report_and_exits_zero(tmp_path, capsys):
    target = write_runs(
        tmp_path,
        [run_summary("run-1"), run_summary("run-2", nodes=["node-a"])],
    )
    output = tmp_path / "bench" / "manifest.json"

    code = bench.main(
        ["--input", str(target), "--output", str(output), "--indent"]
    )

    assert code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["tool"] == "prism-prompt-benchmark"
    assert report["schema_version"] == 1
    assert str(tmp_path) not in output.read_text(encoding="utf-8")
    stderr = capsys.readouterr().err
    assert "stability=unstable" in stderr
    assert "runs=2" in stderr


def test_cli_usage_error_without_input(tmp_path, capsys):
    assert bench.main([]) == 2
    assert "usage-error" in capsys.readouterr().err
