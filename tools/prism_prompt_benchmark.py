#!/usr/bin/env python3
"""Offline stability benchmark across sanitized PRISM prompt-profile runs.

Reads already-sanitized per-run JSON summaries (one file per extraction
run) and computes, per ``profile`` × ``case``, how stable the runs'
outcomes are: candidate ids and types per kind (node, temporal_fact,
claim, conflict, relation), gap types, coverage metrics and the run
verdict statuses — with intersection, union and per-item frequency across
repeated runs.

Policy (deliberate, non-negotiable):

* Node counts are never a success criterion.  A profile is judged only by
  cross-run agreement of ids/types/gaps/coverage, never by how many
  candidates it produced.
* No case-specific expected relations are assumed, so missing relations
  never fail a profile; an empty relation union is "not applicable".
* Mechanism and semantic verdicts are reported as explicitly separate
  sections; neither is collapsed into the stability verdict.
* Real provider execution is not implemented.  This tool is offline-only:
  it reads local JSON files and never calls an LLM, the network, or a
  Graphiti adapter.  Wiring live runs would require an explicit opt-in
  flag that intentionally does not exist here.

Input contract
--------------
Each input file is one JSON object describing one run of one prompt
profile over one case, already sanitized upstream (ids and closed
vocabularies only — no material body, quotes, prompts, absolute paths or
secrets; the reader rejects such fields and over-long prose values):

``profile``, ``run_id``, ``case_id`` (required safe labels); ``candidates``
mapping each known kind to ``{"ids": [...], "types": {...}}``; optional
``gap_types`` (label → count), ``coverage`` (label → 0..1 rate) and
``verdict`` with ``mechanism_status`` / ``semantic_status`` in
pass/partial/fail.

Exit codes: ``0`` report produced; ``2`` usage/input errors; ``3``
structural data or sanitization errors.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
TOOL_NAME = "prism-prompt-benchmark"

CANDIDATE_KINDS = ("node", "temporal_fact", "claim", "conflict", "relation")
VERDICT_STATUSES = ("pass", "partial", "fail")

_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "tool",
        "profile",
        "run_id",
        "case_id",
        "candidates",
        "gap_types",
        "coverage",
        "verdict",
    }
)
_REQUIRED_TOP_LEVEL = frozenset({"profile", "run_id", "case_id", "candidates"})

# A sanitized run summary is ids, types, counts, rates and statuses.  These
# keys can only appear when upstream sanitization failed, so their presence
# is a data error, never a silent redaction.
_MATERIAL_LIKE_KEYS = frozenset(
    {
        "quote",
        "quotes",
        "content",
        "body",
        "text",
        "prompt",
        "excerpt",
        "passage",
        "material",
        "materials",
        "path",
        "corpus_path",
        "api_key",
        "password",
        "secret",
        "token",
    }
)

# Labels (ids, case/profile names, type and gap vocabulary) must stay
# opaque, portable and provably path-free before they may appear in a
# report.
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_STRING = 256
_MAX_REPORT_STRING = 200

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


class BenchmarkError(Exception):
    """Operational failure with an explicit, user-facing classification."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# ------------------------------------------------------------------- helpers


def _safe_label(path: str, value: object) -> str:
    if isinstance(value, str) and _SAFE_LABEL.match(value):
        return value
    raise BenchmarkError(
        "data", f"{path} must be a safe label (got {value!r})"
    )


def _walk_summary(node: object, path: str) -> None:
    """Reject non-JSON-safe data, material-like fields and prose values."""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise BenchmarkError("data", f"{path} keys must be strings")
            if key in _MATERIAL_LIKE_KEYS:
                raise BenchmarkError(
                    "data",
                    f"{path}.{key} is a material-like or secret-bearing field; "
                    "run summaries must already be sanitized to ids and "
                    "closed vocabularies",
                )
            _walk_summary(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_summary(item, f"{path}[{index}]")
    elif isinstance(node, str):
        if len(node) > _MAX_STRING:
            raise BenchmarkError(
                "data",
                f"{path} exceeds {_MAX_STRING} characters and looks like "
                "prose or material content",
            )
    elif node is None or isinstance(node, bool) or isinstance(node, (int, float)):
        return
    else:
        raise BenchmarkError("data", f"{path} must contain only JSON-safe values")


def _count_map(path: str, value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise BenchmarkError("data", f"{path} must be an object")
    result: dict[str, int] = {}
    for key, count in value.items():
        label = _safe_label(f"{path} key", key)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise BenchmarkError("data", f"{path}.{label} must be a non-negative integer")
        result[label] = count
    return result


def _rate_map(path: str, value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        raise BenchmarkError("data", f"{path} must be an object")
    result: dict[str, float] = {}
    for key, rate in value.items():
        label = _safe_label(f"{path} key", key)
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise BenchmarkError("data", f"{path}.{label} must be a number")
        if not 0.0 <= rate <= 1.0:
            raise BenchmarkError("data", f"{path}.{label} must be between 0.0 and 1.0")
        result[label] = float(rate)
    return result


def _verdict_status(path: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in VERDICT_STATUSES:
        allowed = ", ".join(VERDICT_STATUSES)
        raise BenchmarkError("data", f"{path} must be one of: {allowed}")
    return value


# ------------------------------------------------------------- run summaries


@dataclass(frozen=True)
class _Run:
    profile: str
    run_id: str
    case_id: str
    ids: dict[str, tuple[str, ...]]
    types: dict[str, dict[str, int]] = field(default_factory=dict)
    gap_types: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, float] = field(default_factory=dict)
    mechanism_status: str | None = None
    semantic_status: str | None = None


def _parse_summary(payload: object, source: Path) -> _Run:
    if not isinstance(payload, dict):
        raise BenchmarkError("data", f"run summary must be a JSON object: {source.name}")
    _walk_summary(payload, "run summary")
    missing = sorted(_REQUIRED_TOP_LEVEL - payload.keys())
    if missing:
        raise BenchmarkError(
            "data",
            f"run summary {source.name} missing required field(s): "
            + ", ".join(missing),
        )
    extra = sorted(payload.keys() - _ALLOWED_TOP_LEVEL)
    if extra:
        raise BenchmarkError(
            "data",
            f"run summary {source.name} has unexpected field(s): " + ", ".join(extra),
        )
    profile = _safe_label("profile", payload["profile"])
    run_id = _safe_label("run_id", payload["run_id"])
    case_id = _safe_label("case_id", payload["case_id"])

    raw_candidates = payload["candidates"]
    if not isinstance(raw_candidates, dict):
        raise BenchmarkError("data", "candidates must be an object")
    unknown_kinds = sorted(raw_candidates.keys() - set(CANDIDATE_KINDS))
    if unknown_kinds:
        raise BenchmarkError(
            "data",
            "candidates has unknown kind(s): " + ", ".join(unknown_kinds),
        )
    ids: dict[str, tuple[str, ...]] = {}
    types: dict[str, dict[str, int]] = {}
    for kind in CANDIDATE_KINDS:
        entry = raw_candidates.get(kind)
        if entry is None:
            ids[kind] = ()
            types[kind] = {}
            continue
        if not isinstance(entry, dict):
            raise BenchmarkError("data", f"candidates.{kind} must be an object")
        entry_keys = set(entry.keys())
        if entry_keys != {"ids", "types"}:
            raise BenchmarkError(
                "data",
                f"candidates.{kind} must have exactly the fields ids and types",
            )
        raw_ids = entry["ids"]
        if not isinstance(raw_ids, list):
            raise BenchmarkError("data", f"candidates.{kind}.ids must be an array")
        labels = [
            _safe_label(f"candidates.{kind}.ids[{index}]", item)
            for index, item in enumerate(raw_ids)
        ]
        if len(set(labels)) != len(labels):
            raise BenchmarkError(
                "data", f"candidates.{kind}.ids contains duplicate ids"
            )
        ids[kind] = tuple(sorted(labels))
        types[kind] = _count_map(f"candidates.{kind}.types", entry["types"])

    gap_types = (
        _count_map("gap_types", payload["gap_types"])
        if "gap_types" in payload
        else {}
    )
    coverage = (
        _rate_map("coverage", payload["coverage"]) if "coverage" in payload else {}
    )
    mechanism_status = semantic_status = None
    verdict = payload.get("verdict")
    if verdict is not None:
        if not isinstance(verdict, dict):
            raise BenchmarkError("data", "verdict must be an object")
        mechanism_status = _verdict_status(
            "verdict.mechanism_status", verdict.get("mechanism_status")
        )
        semantic_status = _verdict_status(
            "verdict.semantic_status", verdict.get("semantic_status")
        )
    return _Run(
        profile=profile,
        run_id=run_id,
        case_id=case_id,
        ids=ids,
        types=types,
        gap_types=gap_types,
        coverage=coverage,
        mechanism_status=mechanism_status,
        semantic_status=semantic_status,
    )


def _collect_files(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in inputs:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        else:
            raise BenchmarkError("input", f"input path not found: {path}")
    if not files:
        raise BenchmarkError("data", "no run summary JSON files found")
    return files


def _load_runs(files: list[Path]) -> list[_Run]:
    runs: list[_Run] = []
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BenchmarkError("input", f"cannot read run summary: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(
                "data", f"run summary {path.name} is not valid JSON: {exc}"
            ) from exc
        run = _parse_summary(payload, path)
        key = (run.profile, run.case_id, run.run_id)
        if key in seen:
            raise BenchmarkError(
                "data",
                f"duplicate run_id {run.run_id!r} for profile {run.profile!r} "
                f"case {run.case_id!r}",
            )
        seen.add(key)
        runs.append(run)
    return runs


# ------------------------------------------------------------------ reducing


def _round4(value: float) -> float:
    return round(value, 4)


def _id_stability(per_run: list[tuple[str, ...]]) -> dict[str, Any]:
    sets = [set(ids) for ids in per_run]
    intersection = set.intersection(*sets) if sets else set()
    union = set.union(*sets) if sets else set()
    frequency = {
        item: sum(item in run_set for run_set in sets) for item in sorted(union)
    }
    if not union:
        status = "not_applicable"
        rate = None
    else:
        rate = _round4(len(intersection) / len(union))
        status = "stable" if intersection == union else "unstable"
    return {
        "intersection": sorted(intersection),
        "union": sorted(union),
        "frequency": frequency,
        "stability_rate": rate,
        "status": status,
        "differing_ids": sorted(union - intersection),
    }


def _type_stability(per_run: list[dict[str, int]]) -> dict[str, Any]:
    sets = [set(map.keys()) for map in per_run]
    intersection = set.intersection(*sets) if sets else set()
    union = set.union(*sets) if sets else set()
    frequency = {
        label: sum(label in labels for labels in sets) for label in sorted(union)
    }
    return {
        "per_run": [dict(sorted(map.items())) for map in per_run],
        "intersection": sorted(intersection),
        "union": sorted(union),
        "frequency": frequency,
    }


def _differing_ids_bounded(ids: list[str], limit: int = 8) -> str:
    if len(ids) <= limit:
        return ", ".join(ids)
    return ", ".join(ids[:limit]) + f" (+{len(ids) - limit} more)"


def _reduce_group(runs: list[_Run]) -> dict[str, Any]:
    runs = sorted(runs, key=lambda run: run.run_id)
    candidates: dict[str, Any] = {}
    unstable_kinds: list[str] = []
    for kind in CANDIDATE_KINDS:
        id_report = _id_stability([run.ids[kind] for run in runs])
        candidates[kind] = {
            "per_run_ids": [list(run.ids[kind]) for run in runs],
            "ids": id_report,
            "types": _type_stability([run.types[kind] for run in runs]),
        }
        if id_report["status"] == "unstable":
            unstable_kinds.append(kind)

    gap_frequency: dict[str, int] = {}
    for run in runs:
        for label in run.gap_types:
            gap_frequency[label] = gap_frequency.get(label, 0) + 1
    coverage: dict[str, dict[str, float]] = {}
    metric_values: dict[str, list[float]] = {}
    for run in runs:
        for metric, rate in run.coverage.items():
            metric_values.setdefault(metric, []).append(rate)
    for metric, values in sorted(metric_values.items()):
        coverage[metric] = {"min": _round4(min(values)), "max": _round4(max(values))}

    def status_counts(attribute: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in runs:
            status = getattr(run, attribute)
            if status is not None:
                counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    if len(runs) < 2:
        stability_status = "insufficient_runs"
    elif unstable_kinds:
        stability_status = "unstable"
    else:
        stability_status = "stable"
    return {
        "runs": len(runs),
        "run_ids": [run.run_id for run in runs],
        "candidates": candidates,
        "gap_types": {
            "per_run": [dict(sorted(run.gap_types.items())) for run in runs],
            "frequency": dict(sorted(gap_frequency.items())),
        },
        "coverage": coverage,
        "mechanism": {"status_counts": status_counts("mechanism_status")},
        "semantic": {"status_counts": status_counts("semantic_status")},
        "stability": {
            "status": stability_status,
            "unstable_kinds": unstable_kinds,
        },
    }


def _guard_sanitized(report: dict[str, Any], forbidden_fragments: set[str]) -> None:
    """Refuse to emit any report string that leaks paths, secrets or prose."""

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
            if len(node) > _MAX_REPORT_STRING:
                problems.add("overlong-string")
            for name, pattern in _PRIVACY_PATTERNS:
                if pattern.search(node):
                    problems.add(name)
            for fragment in forbidden_fragments:
                if fragment and fragment in node:
                    problems.add("input-path-fragment")

    visit(report)
    if problems:
        raise BenchmarkError(
            "sanitization",
            "report strings matched privacy guard patterns: "
            + ", ".join(sorted(problems)),
        )


_POLICY = {
    "success_criteria": (
        "cross-run agreement of candidate ids and types, gap types, "
        "coverage metrics and verdict statuses per profile and case"
    ),
    "node_counts_are_not_a_success_criterion": True,
    "missing_relations_do_not_fail": (
        "no case-specific expected relations are assumed; an empty relation "
        "union is not applicable, never a failure"
    ),
    "live_provider_execution": (
        "not implemented; this tool reads offline sanitized summaries only "
        "and never calls an LLM provider"
    ),
}


def build_report(inputs: list[Path]) -> dict[str, Any]:
    """Assemble the sanitized stability report from run-summary inputs."""
    files = _collect_files(inputs)
    runs = _load_runs(files)

    groups: dict[tuple[str, str], list[_Run]] = {}
    for run in runs:
        groups.setdefault((run.profile, run.case_id), []).append(run)

    profiles_block: dict[str, Any] = {}
    reasons: list[str] = []
    any_unstable = False
    any_insufficient = False
    for (profile, case_id) in sorted(groups):
        group_runs = groups[(profile, case_id)]
        reduced = _reduce_group(group_runs)
        profiles_block.setdefault(profile, {}).setdefault("cases", {})[case_id] = (
            reduced
        )
        for kind in reduced["stability"]["unstable_kinds"]:
            differing = reduced["candidates"][kind]["ids"]["differing_ids"]
            reasons.append(
                f"profile {profile} case {case_id}: kind {kind} candidate ids "
                f"differ across runs ({_differing_ids_bounded(differing)})"
            )
        if reduced["stability"]["status"] == "unstable":
            any_unstable = True
        elif reduced["stability"]["status"] == "insufficient_runs":
            any_insufficient = True
            reasons.append(
                f"profile {profile} case {case_id}: {reduced['runs']} run(s) "
                "only; stability needs at least 2 runs"
            )

    profile_names = sorted({run.profile for run in runs})
    for profile in profile_names:
        profile_runs = [run for run in runs if run.profile == profile]
        block = profiles_block[profile]
        block["runs"] = len(profile_runs)
        block["runs_without_verdict"] = sum(
            run.mechanism_status is None and run.semantic_status is None
            for run in profile_runs
        )
        for attribute, name in (
            ("mechanism_status", "mechanism"),
            ("semantic_status", "semantic"),
        ):
            counts: dict[str, int] = {}
            with_verdict = 0
            for run in profile_runs:
                status = getattr(run, attribute)
                if status is not None:
                    with_verdict += 1
                    counts[status] = counts.get(status, 0) + 1
            block[name] = {
                "status_counts": dict(sorted(counts.items())),
                "runs_with_verdict": with_verdict,
            }

    if any_unstable:
        stability_status = "unstable"
    elif any_insufficient:
        stability_status = "insufficient_runs"
    else:
        stability_status = "stable"

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "run_summaries": len(runs),
            "profiles": profile_names,
            "cases": len(groups),
        },
        "policy": _POLICY,
        "profiles": profiles_block,
        "verdict": {
            "stability_status": stability_status,
            "reasons": reasons,
        },
    }
    forbidden: set[str] = set()
    for path in files:
        forbidden.update({str(path), str(path.resolve()), path.resolve().as_posix()})
    for path in inputs:
        forbidden.update({str(path), str(path.resolve()), path.resolve().as_posix()})
    _guard_sanitized(report, forbidden)
    return report


# ------------------------------------------------------------------------ CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BenchmarkError("usage", message)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(
        prog=TOOL_NAME,
        description="Offline stability benchmark across sanitized "
        "prompt-profile run summaries.",
    )
    parser.add_argument(
        "--input",
        action="append",
        dest="inputs",
        required=True,
        help="run summary JSON file or directory of *.json summaries "
        "(repeatable)",
    )
    parser.add_argument(
        "--output", help="write the JSON report here (default: stdout)"
    )
    parser.add_argument(
        "--indent", action="store_true", help="pretty-print the JSON report"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        report = build_report([Path(item) for item in args.inputs])
    except BenchmarkError as exc:
        print(f"{exc.kind}-error: {exc.message}", file=sys.stderr)
        return 2 if exc.kind in ("usage", "input") else 3

    if args.output:
        output_path = Path(args.output)
        try:
            _guard_sanitized(
                report,
                {str(output_path), str(output_path.resolve())},
            )
        except BenchmarkError as exc:
            print(f"{exc.kind}-error: {exc.message}", file=sys.stderr)
            return 3

    rendered = json.dumps(
        report,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        indent=2 if args.indent else None,
    )
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    else:
        sys.stdout.write(rendered + "\n")
    print(
        f"{TOOL_NAME}: stability={report['verdict']['stability_status']}"
        f" runs={report['inputs']['run_summaries']}"
        f" profiles={len(report['inputs']['profiles'])}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
