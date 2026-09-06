#!/usr/bin/env python3
"""Reproducible acceptance runner for one real Graphiti/provider case.

The runner is deliberately split into two layers:

* an offline, testable contract layer (path/material selection, loopback and
  environment preflight, summary sanitization);
* an opt-in execution layer that uses the normal PRISM composition root, a
  real GraphitiBackend, the configured DeepSeek-compatible provider, a fresh
  runtime restart, two historical cutoffs, and the existing report ledger.

It never starts or stops Neo4j/Graphiti.  If the PRISM-owned loopback service
is not listening, it reports BLOCKED instead of attempting to start one.  The
runner writes only to its caller-supplied output directory.  The public
JSON/Markdown artifacts contain counts, statuses and safe labels, never
material bodies, credentials, or absolute paths.

Usage (paths are intentionally explicit; no private path is embedded here):

    python tools/run_live_case_acceptance.py \
        --source-root <PRISM material workspace> \
        --output-dir <acceptance output directory> \
        [--prompt-profile baseline|protocol-v1] [--run-id <safe-label>]

Required environment variable names (not values) are configured by the
runner: DEEPSEEK_API_KEY and PRISM_GRAPHITI_PASSWORD.  The Graphiti Bolt URI
defaults to PRISM_GRAPHITI_URI and must be loopback port 7688; the HTTP
precheck defaults to http://127.0.0.1:7475.

``--prompt-profile`` selects the extraction prompt profile for the runtime
the runner itself composes (default ``baseline``; unknown values fail
closed).  On a pass/partial run the runner also writes a strictly
sanitized ``prompt-run-summary.json`` bridge next to the acceptance
summary, projected from THIS run's own SQLite ``case_extraction_ledger``
and quality results, directly consumable by ``prism_prompt_benchmark.py``.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys
from typing import Any
from urllib.parse import quote, urlsplit

from prism.config import (
    GraphitiConfig,
    LLMConfig,
    LLMProviderConfig,
    PathConfig,
    PrismConfig,
)
from prism.domain import EvolutionCase
from prism.extraction import KNOWN_PROMPT_PROFILES
from prism.graph import GraphitiBackend
from prism.graph.graphiti_client import build_graphiti_client
from prism.runtime import create_runtime

SCHEMA_VERSION = 1
TOOL_NAME = "prism-live-case-acceptance"

PROMPT_PROFILE_BASELINE = "baseline"
BRIDGE_FILENAME = "prompt-run-summary.json"
BRIDGE_SCHEMA_VERSION = 1
_BRIDGE_VERDICT_STATUSES = ("pass", "partial", "fail")

CASE_ID = "beijing-housing-policy-evolution"
CASE_TYPE = "policy"
CASE_NAME = "Beijing housing and housing-provident-fund policy evolution"
CASE_START_AT = datetime(2025, 12, 24, tzinfo=timezone.utc)

MATERIAL_IDS = (
    "mat_6cba3cefb6f2b4bf3735a3d0",
    "mat_49419161b2768d1d2c873d4b",
    "mat_ea956158ecd4de13e160f730",
    "mat_b5f7296bf770bb6c59b9015b",
)
MATERIAL_COUNT = len(MATERIAL_IDS)

CUTOFFS = (
    (
        "before-materials",
        datetime(2026, 8, 1, tzinfo=timezone.utc),
    ),
    (
        "after-materials",
        datetime(2026, 9, 3, tzinfo=timezone.utc),
    ),
)

GRAPHITI_DATABASE = "neo4j"
GRAPHITI_GROUP_ID = "neo4j"
DEFAULT_BOLT_URI = "bolt://127.0.0.1:7688"
DEFAULT_HTTP_URI = "http://127.0.0.1:7475"
DEFAULT_PASSWORD_ENV = "PRISM_GRAPHITI_PASSWORD"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_PROVIDER_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_PROVIDER_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_PROVIDER_MODEL = "deepseek-chat"

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PRIVACY_PATTERNS = (
    ("windows-drive-path", re.compile(r"[A-Za-z]:[/\\]")),
    ("unc-path", re.compile(r"\\\\")),
    ("posix-private-path", re.compile(r"(?<![\w.])/(?:home|Users|root|tmp|var)/")),
    (
        "secret-like-wording",
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|credential|password|passwd"
            r"|secret|token|bearer)\b"
        ),
    ),
)


class AcceptanceError(Exception):
    """Base class for classified runner failures."""

    kind = "acceptance"


class AcceptanceInputError(AcceptanceError):
    kind = "input"


class AcceptanceBlockedError(AcceptanceError):
    kind = "blocked"

    def __init__(self, message: str, checks: Mapping[str, bool] | None = None) -> None:
        super().__init__(message)
        self.checks = dict(checks or {})


class AcceptanceRuntimeError(AcceptanceError):
    kind = "runtime"


class SanitizationError(AcceptanceError):
    kind = "sanitization"


@dataclass(frozen=True, slots=True)
class MaterialFile:
    """A selected input material, represented without exposing its body."""

    material_id: str
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PreflightResult:
    status: str
    reasons: tuple[str, ...]
    checks: dict[str, bool]

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass(slots=True)
class RunOptions:
    material_files: tuple[MaterialFile, ...]
    output_dir: Path
    llm_provider_name: str
    llm_api_key_env: str
    llm_base_url: str
    llm_model: str
    bolt_uri: str
    http_uri: str
    graphiti_password_env: str
    provider: str
    graphiti_database: str = GRAPHITI_DATABASE
    graphiti_group_id: str = GRAPHITI_GROUP_ID
    graphiti_model_max_tokens: int = 4096
    report_llm: bool = False
    strict: bool = False
    prompt_profile: str = PROMPT_PROFILE_BASELINE
    run_id: str | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_label(name: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        raise AcceptanceInputError(f"{name} must be a safe opaque label")
    return value


def resolve_prompt_profile(value: object) -> str:
    """Map a profile selection to its safe label, or fail closed.

    ``None`` and ``"baseline"`` denote the untouched baseline prompt; any
    other value must be a known profile name — a typo can never silently
    run the baseline.
    """

    if value is None:
        return PROMPT_PROFILE_BASELINE
    if isinstance(value, str) and value in KNOWN_PROMPT_PROFILES:
        return value
    allowed = ", ".join(sorted(KNOWN_PROMPT_PROFILES))
    raise AcceptanceInputError(
        f"unknown prompt profile {value!r}; known profiles: {allowed}"
    )


def resolve_run_id(explicit: str | None) -> str:
    """Return a stable, readable, safe-label run id.

    An explicit id must already be a safe label.  The automatic id is a UTC
    timestamp plus a random token — deliberately NOT derived from any
    private path, material content or environment value.
    """

    if explicit is not None:
        return _require_label("run id", explicit)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(3)}"


def collect_material_files(
    source_root: str | os.PathLike[str],
    *,
    material_ids: Sequence[str] = MATERIAL_IDS,
) -> tuple[MaterialFile, ...]:
    """Collect exactly one corpus Markdown file for each selected material."""

    root = Path(source_root)
    corpus = root / "corpus"
    if not corpus.is_dir():
        raise AcceptanceInputError("source root does not contain a corpus directory")

    collected: list[MaterialFile] = []
    for material_id in material_ids:
        _require_label("material id", material_id)
        matches = sorted(corpus.glob(f"*-{material_id}.md"))
        if len(matches) != 1:
            raise AcceptanceInputError(
                f"material file not found or ambiguous for {material_id}"
            )
        path = matches[0].resolve()
        if not path.is_relative_to(corpus.resolve()):
            raise AcceptanceInputError("material path escapes the source corpus")
        collected.append(
            MaterialFile(
                material_id=material_id,
                path=path,
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
            )
        )
    return tuple(collected)


def _loopback_host(host: str | None) -> bool:
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _uri_parts(name: str, uri: str) -> tuple[str, int | None]:
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError as error:
        raise AcceptanceInputError(f"{name} is not parseable") from error
    if not parts.scheme or not parts.hostname:
        raise AcceptanceInputError(f"{name} must include scheme and host")
    return parts.hostname, port


def _socket_open(host: str, port: int, *, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def preflight_live_services(
    bolt_uri: str,
    http_uri: str,
    graphiti_password_env: str,
    provider_api_key_env: str,
    probe: Callable[..., bool] | None = None,
    timeout: float = 1.0,
    environ: Mapping[str, str] | None = None,
) -> PreflightResult:
    """Check prerequisites without starting, stopping, or writing to services.

    Only variable NAMES are accepted.  Values are checked for presence and
    never copied into a report.  The default probe is a short loopback TCP
    connect, which cannot modify the service.
    """

    if not bolt_uri:
        return PreflightResult(
            "blocked", ("Graphiti Bolt URI is required",), {"bolt_uri": False}
        )
    if not http_uri:
        return PreflightResult(
            "blocked", ("Graphiti HTTP URI is required",), {"http_uri": False}
        )

    bolt_host, bolt_port = _uri_parts("Graphiti Bolt URI", bolt_uri)
    checks: dict[str, bool] = {"bolt_loopback": _loopback_host(bolt_host)}
    if not checks["bolt_loopback"]:
        return PreflightResult(
            "blocked", ("Graphiti Bolt URI host must be loopback",), checks
        )
    checks["bolt_loopback"] = bolt_port == 7688
    if bolt_port != 7688:
        return PreflightResult(
            "blocked", ("Graphiti Bolt URI port must be 7688",), checks
        )

    http_host, http_port = _uri_parts("Graphiti HTTP URI", http_uri)
    checks["http_loopback"] = _loopback_host(http_host) and http_port == 7475
    if not checks["http_loopback"]:
        return PreflightResult(
            "blocked", ("Graphiti HTTP endpoint must be loopback port 7475",), checks
        )

    environment = os.environ if environ is None else environ
    effective_probe = probe or _socket_open
    http_open = effective_probe(http_host, 7475, timeout=timeout)
    bolt_open = effective_probe(bolt_host, 7688, timeout=timeout)
    checks["graphiti_password_env"] = bool(environment.get(graphiti_password_env))
    checks["provider_api_key_env"] = bool(environment.get(provider_api_key_env))
    reasons: list[str] = []
    if not checks["graphiti_password_env"]:
        reasons.append(f"{graphiti_password_env} is not set")
    if not checks["provider_api_key_env"]:
        reasons.append(f"{provider_api_key_env} is not set")
    checks["http_loopback"] = http_open
    checks["bolt_loopback"] = bolt_open
    if not http_open:
        reasons.append("Graphiti HTTP endpoint is not listening on loopback")
    if not bolt_open:
        reasons.append("Graphiti Bolt endpoint is not listening on loopback")
    if reasons:
        return PreflightResult("blocked", tuple(reasons), checks)
    return PreflightResult("ready", (), checks)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    path.write_text(rendered + "\n", encoding="utf-8")


def _state_payload(options: RunOptions) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": CASE_ID,
        "materials": [
            {
                "material_id": item.material_id,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in options.material_files
        ],
    }


def _build_config(options: RunOptions) -> PrismConfig:
    provider_name = _require_label("LLM provider name", options.llm_provider_name)
    _require_label("LLM API variable name", options.llm_api_key_env)
    _require_label("Graphiti password variable name", options.graphiti_password_env)
    task_roles = {"extract": provider_name}
    if options.report_llm:
        task_roles["summarize_report"] = provider_name
    return PrismConfig(
        paths=PathConfig(),
        llm=LLMConfig(
            providers={
                provider_name: LLMProviderConfig(
                    model=options.llm_model,
                    api_key_env=options.llm_api_key_env,
                    base_url=options.llm_base_url,
                    timeout=120.0,
                    concurrency_limit=1,
                )
            },
            task_roles=task_roles,
        ),
        graphiti=GraphitiConfig(
            enabled=True,
            uri=options.bolt_uri,
            database=options.graphiti_database,
            group_id=options.graphiti_group_id,
            password_env=options.graphiti_password_env,
            timeout=30.0,
        ),
    )


def prepare_run_home(options: RunOptions) -> tuple[Path, Path]:
    """Create or validate an idempotent, output-local PRISM home."""

    options.output_dir.mkdir(parents=True, exist_ok=True)
    home = options.output_dir / "prism-home"
    home.mkdir(parents=True, exist_ok=True)

    state_path = options.output_dir / "run-state.json"
    payload = _state_payload(options)
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AcceptanceInputError("run state is unreadable") from error
        if existing != payload:
            raise AcceptanceInputError("material fingerprints differ from run state")
    else:
        _write_json(state_path, payload)

    config_path = home / "config.json"
    _build_config(options).save(config_path)
    return home, config_path


def _optional_dependencies_available() -> bool:
    return (
        importlib.util.find_spec("graphiti_core") is not None
        and importlib.util.find_spec("neo4j") is not None
    )


async def _await_result(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _material_record(runtime: Any, item: MaterialFile) -> dict[str, Any]:
    outcome = runtime.pipeline.outcome_for(item.material_id)
    if getattr(outcome, "status", None) == "committed":
        ingestion = runtime.ingestion.ingest(
            item.path, metadata={"case_tags": [CASE_ID]}
        )
        runtime.store.index_file(ingestion.corpus_path)
        return {
            "material_id": item.material_id,
            "status": "reused",
            "pipeline_status": "committed",
            "case_id": CASE_ID,
            "warnings": 0,
            "error_type": None,
        }

    result = await runtime.api.process_material(
        item.path,
        {"case_tags": [CASE_ID]},
        target_case=EvolutionCase(
            CASE_ID,
            CASE_TYPE,
            CASE_NAME,
            CASE_START_AT,
            "active",
        ),
    )
    pipeline_status = getattr(getattr(result, "pipeline", None), "status", None)
    case_outcome = getattr(result, "case_outcome", None)
    case_id = getattr(case_outcome, "case_id", None)
    write = getattr(case_outcome, "write", None)
    record = {
        "material_id": item.material_id,
        "status": (
            "committed"
            if pipeline_status == "completed" and case_id == CASE_ID
            else "failed"
        ),
        "pipeline_status": pipeline_status,
        "case_id": case_id,
        "warnings": len(getattr(result, "warnings", ()) or ()),
        "error_type": None,
    }
    if write is not None:
        record["graph_added"] = len(getattr(write, "added_keys", ()) or ())
        record["graph_skipped"] = len(getattr(write, "skipped_keys", ()) or ())
    return record


async def _process_materials(
    runtime: Any, materials: Sequence[MaterialFile]
) -> tuple[dict[str, Any], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    stage_counts: dict[str, int] = {}
    for item in materials:
        try:
            record = await _material_record(runtime, item)
        except Exception as error:
            record = {
                "material_id": item.material_id,
                "status": "failed",
                "pipeline_status": None,
                "case_id": None,
                "warnings": 0,
                "error_type": type(error).__name__,
            }
            stage = getattr(error, "stage", None)
            if isinstance(stage, str):
                record["failed_stage"] = _require_label("failed stage", stage)
        records.append(record)
        run = getattr(runtime.pipeline, "run_for", lambda material_id: None)(
            item.material_id
        )
        for stage in getattr(run, "stages", ()) or ():
            key = f"{getattr(stage, 'name', 'unknown')}:{getattr(stage, 'status', 'unknown')}"
            stage_counts[key] = stage_counts.get(key, 0) + 1
    return {"records": records}, stage_counts


def runtime_materials(runtime: Any) -> tuple[MaterialFile, ...]:
    return getattr(runtime, "_acceptance_materials", ())


def _pipeline_summary(records: Sequence[dict[str, Any]], stage_counts: Mapping[str, int], dispatch_errors: int) -> dict[str, Any]:
    committed = sum(item["status"] == "committed" for item in records)
    reused = sum(item["status"] == "reused" for item in records)
    failed = sum(item["status"] == "failed" for item in records)
    return {
        "total": len(records),
        "processed": committed + reused,
        "committed": committed,
        "reused": reused,
        "failed": failed,
        "records": [
            {
                "material_id": item["material_id"],
                "status": item["status"],
                "pipeline_status": item["pipeline_status"],
                "case_id": item["case_id"],
                "warnings": item["warnings"],
                "error_type": item["error_type"],
                **(
                    {"graph_added": item["graph_added"]}
                    if isinstance(item.get("graph_added"), int)
                    else {}
                ),
                **(
                    {"graph_skipped": item["graph_skipped"]}
                    if isinstance(item.get("graph_skipped"), int)
                    else {}
                ),
                **({"failed_stage": item["failed_stage"]} if item.get("failed_stage") else {}),
            }
            for item in records
        ],
        "stage_counts": dict(sorted(stage_counts.items())),
        "dispatch_errors": dispatch_errors,
    }


def _default_quality_gate(
    home: Path,
    materials: Mapping[str, Any],
    *,
    case_id: str = CASE_ID,
) -> dict[str, Any]:
    _require_label("quality gate case id", case_id)
    run_summary = {
        "case_id": case_id,
        "input_files": materials["total"],
        "materials": {
            "successful": materials["processed"],
            "failed": materials["failed"],
        },
    }
    _write_json(home / "run-summary.json", run_summary)
    report_path = home.parent / "quality-gate.json"
    tools_dir = Path(__file__).resolve().parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import prism_quality_gate as gate  # noqa: PLC0415

    try:
        gate.main(
            [
                "--run-dir",
                str(home),
                "--case-id",
                case_id,
                "--output",
                str(report_path),
                "--indent",
            ]
        )
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as error:
        return {
            "verdict": {
                "mechanism_status": "fail",
                "semantic_status": "fail",
                "reasons": [f"quality gate unavailable: {type(error).__name__}"],
            },
            "substantive": {},
            "evidence_gaps": {},
            "coverage": {},
        }


def _ensure_graphiti_runtime(runtime: Any) -> None:
    if not isinstance(runtime.graph_backend, GraphitiBackend):
        raise AcceptanceRuntimeError(
            "live acceptance requires the real GraphitiBackend, not OfflineGraphBackend"
        )


# ------------------------------------------------------- prompt-run bridge

#: How each candidate kind is projected from a ledger extraction payload:
#: (kind, collection, id field, type field).  Ids are model-provided free
#: text, so only safe-label values are ever emitted; type fields are closed
#: domain vocabularies counted with an "other"/"unset" fallback.
_CANDIDATE_PROJECTIONS = (
    ("node", "nodes", "id", "node_type"),
    ("temporal_fact", "temporal_facts", "fact_id", "provenance_type"),
    ("claim", "claims", "claim_id", "claim_type"),
    ("conflict", "conflicts", "conflict_id", "provenance_type"),
    ("relation", "relations", "relation_id", "relation_type"),
)


def _safe_id(value: object) -> str | None:
    """The value itself when it is a safe opaque label, else ``None``."""

    if isinstance(value, str) and _SAFE_LABEL.fullmatch(value):
        return value
    return None


def _dist_label(dist: dict[str, int], value: object) -> None:
    if value is None:
        key = "unset"
    elif isinstance(value, str) and _SAFE_LABEL.fullmatch(value):
        key = value
    else:
        key = "other"
    dist[key] = dist.get(key, 0) + 1


def read_case_extractions(home: Path, case_id: str) -> tuple[dict[str, Any], ...]:
    """Read THIS run's own home ledger rows for one case, read-only.

    The bridge never scans other directories, other databases or old runs:
    it opens exactly ``<home>/data/index.db`` in read-only mode and selects
    this run's target case rows in first-recorded order.
    """

    _require_label("case id", case_id)
    database = home / "data" / "index.db"
    if not database.is_file():
        raise AcceptanceRuntimeError(
            "run home has no index.db case ledger to project"
        )
    uri = "file:" + quote(database.resolve().as_posix()) + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise AcceptanceRuntimeError(
            "case ledger database is unreadable"
        ) from error
    try:
        rows = connection.execute(
            "SELECT extraction_json FROM case_extraction_ledger "
            "WHERE case_id = ? ORDER BY recorded_at, rowid",
            (case_id,),
        ).fetchall()
    except sqlite3.Error as error:
        raise AcceptanceRuntimeError(
            "case_extraction_ledger is unreadable"
        ) from error
    finally:
        connection.close()
    extractions: list[dict[str, Any]] = []
    for (extraction_json,) in rows:
        try:
            payload = json.loads(extraction_json)
        except (TypeError, ValueError) as error:
            raise AcceptanceRuntimeError(
                "case ledger extraction row is not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise AcceptanceRuntimeError(
                "case ledger extraction row is not a JSON object"
            )
        extractions.append(payload)
    return tuple(extractions)


def _bridge_coverage(quality: Mapping[str, Any]) -> dict[str, float]:
    raw = quality.get("coverage")
    coverage: dict[str, float] = {}
    if not isinstance(raw, Mapping):
        return coverage
    for metric, entry in raw.items():
        if not isinstance(metric, str) or not _SAFE_LABEL.fullmatch(metric):
            continue
        if not isinstance(entry, Mapping):
            continue
        rate = entry.get("rate")
        if (
            isinstance(rate, (int, float))
            and not isinstance(rate, bool)
            and 0.0 <= float(rate) <= 1.0
        ):
            coverage[metric] = round(float(rate), 4)
    return dict(sorted(coverage.items()))


def _bridge_verdict(quality: Mapping[str, Any]) -> dict[str, str]:
    raw = quality.get("verdict")
    source = raw if isinstance(raw, Mapping) else {}
    verdict: dict[str, str] = {}
    for name in ("mechanism_status", "semantic_status"):
        status = source.get(name)
        verdict[name] = (
            status if status in _BRIDGE_VERDICT_STATUSES else "fail"
        )
    return verdict


def build_prompt_run_summary(
    *,
    profile: str,
    run_id: str,
    case_id: str,
    extractions: Sequence[Mapping[str, Any]],
    quality: Mapping[str, Any],
) -> dict[str, Any]:
    """Project ledger extractions plus quality results into a sanitized
    per-run summary that ``prism_prompt_benchmark.py`` reads directly.

    Only ids, closed-vocabulary type labels, gap-type counts, coverage
    rates and verdict statuses are emitted — never material content,
    quotes, candidate payloads, corpus or absolute paths, secrets or any
    prompt text.  Candidate ids that are not safe opaque labels (prose,
    paths, over-long strings) are dropped rather than sanitized in place.
    """

    profile = _require_label("profile", profile)
    run_id = _require_label("run id", run_id)
    case_id = _require_label("case id", case_id)
    ids: dict[str, set[str]] = {}
    types: dict[str, dict[str, int]] = {}
    gap_types: dict[str, int] = {}
    for kind, _, _, _ in _CANDIDATE_PROJECTIONS:
        ids[kind] = set()
        types[kind] = {}
    for extraction in extractions:
        if not isinstance(extraction, Mapping):
            raise AcceptanceRuntimeError(
                "ledger extraction row must be a JSON object"
            )
        for kind, collection, id_field, type_field in _CANDIDATE_PROJECTIONS:
            items = extraction.get(collection)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                label = _safe_id(item.get(id_field))
                if label is not None:
                    ids[kind].add(label)
                _dist_label(types[kind], item.get(type_field))
        gaps = extraction.get("evidence_gaps")
        if isinstance(gaps, list):
            for gap in gaps:
                if isinstance(gap, Mapping):
                    _dist_label(gap_types, gap.get("gap_type"))
    return {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "profile": profile,
        "run_id": run_id,
        "case_id": case_id,
        "candidates": {
            kind: {
                "ids": sorted(ids[kind]),
                "types": dict(sorted(types[kind].items())),
            }
            for kind, _, _, _ in _CANDIDATE_PROJECTIONS
        },
        "gap_types": dict(sorted(gap_types.items())),
        "coverage": _bridge_coverage(quality),
        "verdict": _bridge_verdict(quality),
    }


def _payload(entry: Any) -> dict[str, Any]:
    raw = getattr(entry, "payload", None)
    if not isinstance(raw, str):
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dist_add(dist: dict[str, int], value: object) -> None:
    key = "unset" if value is None else str(value)
    if not _SAFE_LABEL.fullmatch(key):
        key = "other"
    dist[key] = dist.get(key, 0) + 1


def _timeline_summary(timeline: Any, cutoff: datetime) -> dict[str, Any]:
    entries = tuple(getattr(timeline, "entries", ()) or ())
    invalidated = tuple(getattr(timeline, "invalidated_entries", ()) or ())
    counts: dict[str, int] = {
        "case": 0,
        "node": 0,
        "fact": 0,
        "claim": 0,
        "relation": 0,
        "material": 0,
    }
    node_type: dict[str, int] = {}
    relation_type: dict[str, int] = {}
    source_total = source_covered = evidence_covered = 0
    temporal_total = valid_at_present = reference_time_present = invalid_at_present = 0
    future_leaks = 0
    for entry in entries:
        kind = str(getattr(entry, "kind", ""))
        normalized = {
            "evolution_case": "case",
            "evolution_node": "node",
            "temporal_fact": "fact",
            "claim": "claim",
            "temporal_relation": "relation",
            "material_provenance": "material",
        }.get(kind, "other")
        counts[normalized] = counts.get(normalized, 0) + 1
        payload = _payload(entry)
        if normalized == "node":
            _dist_add(node_type, payload.get("node_type"))
        if normalized == "relation":
            _dist_add(relation_type, payload.get("relation_type"))
        if normalized not in {"case", "material", "other"}:
            source_total += 1
            source_ids = getattr(entry, "source_ids", ()) or ()
            if source_ids:
                source_covered += 1
            if getattr(entry, "evidence", ()) or ():
                evidence_covered += 1
            temporal_total += 1
            if getattr(entry, "valid_at", None) is not None:
                valid_at_present += 1
            if getattr(entry, "reference_time", None) is not None:
                reference_time_present += 1
            if getattr(entry, "invalid_at", None) is not None:
                invalid_at_present += 1
        reference_time = getattr(entry, "reference_time", None)
        valid_at = getattr(entry, "valid_at", None)
        if (reference_time is not None and reference_time > cutoff) or (
            valid_at is not None and valid_at > cutoff
        ):
            future_leaks += 1
    return {
        "entry_counts": counts,
        "invalidated_fact_count": sum(
            str(getattr(entry, "kind", "")) == "temporal_fact" for entry in invalidated
        ),
        "node_type": dict(sorted(node_type.items())),
        "relation_type": dict(sorted(relation_type.items())),
        "source_evidence_coverage": {
            "source_ids": {
                "numerator": source_covered,
                "denominator": source_total,
            },
            "evidence": {
                "numerator": evidence_covered,
                "denominator": source_total,
            },
        },
        "temporal_fields": {
            "eligible_entries": temporal_total,
            "valid_at": valid_at_present,
            "reference_time": reference_time_present,
            "invalid_at_recorded": invalid_at_present,
        },
        "future_leak_count": future_leaks,
    }


def _error_type(error: BaseException) -> str:
    return type(error).__name__


def _pdf_unavailable_reason(error: Exception) -> str:
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return "optional PDF dependency or renderer unavailable"
    return f"export failed: {type(error).__name__}"


def guard_public_summary(
    report: Mapping[str, Any],
    *,
    forbidden_fragments: Iterable[str] = (),
) -> None:
    """Fail closed if any public report string can leak a private path or secret."""

    problems: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                visit(key)
                visit(value)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                visit(item)
        elif isinstance(node, str):
            for name, pattern in _PRIVACY_PATTERNS:
                if pattern.search(node):
                    problems.add(name)
            for fragment in forbidden_fragments:
                if fragment and fragment in node:
                    problems.add("caller-input-path-fragment")

    visit(report)
    if problems:
        raise SanitizationError(
            "public report matched privacy guard patterns: " + ", ".join(sorted(problems))
        )


def render_markdown_summary(summary: Mapping[str, Any]) -> str:
    materials = summary["materials"]
    verdict = summary["verdict"]
    cutoffs = summary["cutoffs"]
    lines = [
        "# PRISM Live Graphiti Acceptance",
        "",
        f"- Overall: **{summary['overall_status'].upper()}**",
        f"- Mechanism: **{verdict['mechanism_status']}**",
        f"- Semantic: **{verdict['semantic_status']}**",
        f"- Case: `{summary['case_id']}`",
        f"- Prompt profile: `{summary.get('prompt_profile', PROMPT_PROFILE_BASELINE)}`",
        f"- Materials: {materials['processed']}/{materials['total']} processed, {materials['failed']} failed",
        f"- Graph backend: {summary['graph_backend']}",
        f"- Fresh restart readback: {'yes' if summary['restart']['registry_readback'] else 'no'}",
        f"- Report version: {'saved' if summary['report']['version_saved'] else 'not saved'}; PDF: {summary['report']['pdf_status']}",
        "",
        "## Cutoffs",
    ]
    for cutoff in cutoffs:
        counts = cutoff["entry_counts"]
        lines.append(
            f"- {cutoff['name']} ({cutoff['as_of']}): case={counts['case']} node={counts['node']} fact={counts['fact']} claim={counts['claim']} relation={counts['relation']}; future leaks={cutoff['future_leak_count']}"
        )
    if verdict["reasons"]:
        lines.extend(["", "## Reasons"])
        lines.extend(f"- {reason}" for reason in verdict["reasons"])
    lines.extend(["", "## Counts"])
    substantive = summary["substantive"]
    lines.append(
        f"- Substantive records: {substantive.get('total', 0)} (nodes={substantive.get('nodes', 0)}, facts={substantive.get('temporal_facts', 0)}, claims={substantive.get('claims', 0)}, relations={substantive.get('relations', 0)})"
    )
    lines.append(f"- Evidence gaps: {summary['gaps'].get('total', 0)}")
    coverage = summary["coverage"]
    lines.append(
        f"- Source coverage: {coverage.get('source_ids', {}).get('rate')}; evidence locator coverage: {coverage.get('evidence_locator', {}).get('rate')}"
    )
    lines.append("")
    return "\n".join(lines)


def write_public_artifacts(
    summary: Mapping[str, Any],
    output_dir: Path,
    *,
    forbidden_fragments: Iterable[str] = (),
) -> None:
    guard_public_summary(summary, forbidden_fragments=forbidden_fragments)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "acceptance-summary.json", summary)
    markdown = render_markdown_summary(summary)
    guard_public_summary({"markdown": markdown}, forbidden_fragments=forbidden_fragments)
    (output_dir / "acceptance-summary.md").write_text(markdown + "\n", encoding="utf-8")


def make_local_deterministic_embedder() -> Any:
    """Build a Graphiti embedder with no network calls or model dependency."""

    from graphiti_core.embedder.client import EMBEDDING_DIM, EmbedderClient

    class LocalDeterministicEmbedder(EmbedderClient):
        async def create(self, input_data: Any) -> list[float]:
            del input_data
            return [1.0] + [0.0] * (EMBEDDING_DIM - 1)

        async def create_batch(
            self, input_data_list: list[str]
        ) -> list[list[float]]:
            return [await self.create(item) for item in input_data_list]

    return LocalDeterministicEmbedder()


def make_local_deterministic_cross_encoder() -> Any:
    """Build a deterministic, order-preserving Graphiti reranker."""

    from graphiti_core.cross_encoder.client import CrossEncoderClient

    class LocalDeterministicCrossEncoder(CrossEncoderClient):
        async def rank(
            self, query: str, passages: list[str]
        ) -> list[tuple[str, float]]:
            del query
            count = max(len(passages), 1)
            return [
                (passage, (count - index) / count)
                for index, passage in enumerate(passages)
            ]

    return LocalDeterministicCrossEncoder()


def build_real_provider_graphiti_factory(
    options: RunOptions,
) -> Callable[[GraphitiConfig], Any]:
    """Build a real Graphiti client using the configured OpenAI-compatible LLM.

    Graphiti's DeepSeek-compatible client is used for its internal entity/edge
    extraction.  The local deterministic embedder and reranker prevent an
    accidental OpenAI embedding/rerank call; the runner reports this split.
    """

    def factory(config: GraphitiConfig) -> Any:
        from graphiti_core.llm_client.config import LLMConfig as GraphitiLLMConfig
        from graphiti_core.llm_client.openai_generic_client import (
            OpenAIGenericClient,
        )

        api_key = os.environ.get(options.llm_api_key_env, "")
        if not api_key:
            raise AcceptanceBlockedError(
                f"{options.llm_api_key_env} is required for the real provider"
            )
        llm_client = OpenAIGenericClient(
            GraphitiLLMConfig(
                api_key=api_key,
                base_url=options.llm_base_url,
                model=options.llm_model,
                temperature=0.0,
                max_tokens=options.graphiti_model_max_tokens,
            ),
            cache=False,
            max_tokens=options.graphiti_model_max_tokens,
            structured_output_mode="json_object",
        )
        return build_graphiti_client(
            config,
            llm_client=llm_client,
            embedder=make_local_deterministic_embedder(),
            cross_encoder=make_local_deterministic_cross_encoder(),
        )

    return factory


async def _create_runtime(
    factory: Callable[[Path], Any], config_path: Path
) -> Any:
    runtime = factory(config_path)
    return await runtime if inspect.isawaitable(runtime) else runtime


async def run_acceptance(
    options: RunOptions,
    *,
    runtime_factory: Callable[[Path], Any] | None = None,
    preflight: Callable[[], PreflightResult] | None = None,
    quality_gate: Callable[[Path, Mapping[str, Any]], Any] | None = None,
    pdf_exporter: Callable[[str, Path], Any] | None = None,
) -> dict[str, Any]:
    """Execute the acceptance flow and return the sanitized public summary."""

    if len(options.material_files) != MATERIAL_COUNT:
        raise AcceptanceInputError("exactly four materials are required")
    _require_label("case id", CASE_ID)
    profile_label = resolve_prompt_profile(options.prompt_profile)
    run_id = resolve_run_id(options.run_id)

    preflight_result = (
        preflight()
        if preflight is not None
        else preflight_live_services(
            bolt_uri=options.bolt_uri,
            http_uri=options.http_uri,
            graphiti_password_env=options.graphiti_password_env,
            provider_api_key_env=options.llm_api_key_env,
        )
    )
    if not preflight_result.ready:
        raise AcceptanceBlockedError(
            "; ".join(preflight_result.reasons), checks=preflight_result.checks
        )

    home, config_path = prepare_run_home(options)
    # Runtime path resolution is anchored by PRISM_HOME.  Keep this acceptance
    # run fully output-local rather than allowing the default ~/.prism home to
    # receive the temporary SQLite/index ledgers.
    os.environ["PRISM_HOME"] = str(home)
    factory = runtime_factory
    if factory is None:
        if not _optional_dependencies_available():
            raise AcceptanceBlockedError(
                "graphiti-core and neo4j optional dependencies are required"
            )
        graphiti_factory = build_real_provider_graphiti_factory(options)

        async def factory(config_path: Path) -> Any:
            return await create_runtime(
                config_path,
                graphiti_client_factory=graphiti_factory,
                prompt_profile=options.prompt_profile,
            )

    failures: list[dict[str, Any]] = []
    first: Any = await _create_runtime(factory, config_path)
    try:
        _ensure_graphiti_runtime(first)
        material_records, stage_counts = await _process_materials(
            first, options.material_files
        )
        first_dispatch_error_count = len(getattr(first, "dispatch_errors", ()) or ())
    finally:
        await first.close()

    materials = _pipeline_summary(
        material_records["records"],
        stage_counts,
        first_dispatch_error_count,
    )
    quality_value = (
        quality_gate(home, materials)
        if quality_gate is not None
        else _default_quality_gate(home, materials)
    )
    quality = await _await_result(quality_value)
    if not isinstance(quality, Mapping):
        raise AcceptanceRuntimeError("quality gate must return a JSON object")
    verdict = quality.get("verdict") if isinstance(quality.get("verdict"), Mapping) else {}
    mechanism_status = verdict.get("mechanism_status", "fail")
    semantic_status = verdict.get("semantic_status", "fail")

    second: Any = await _create_runtime(factory, config_path)

    restart_merge_added = 0
    restart_merge_skipped = 0
    registry_readback = False
    cutoff_results: list[dict[str, Any]] = []
    report_saved = False
    report_version_id: str | None = None
    report_markdown_chars = 0
    report_summary_origin: str | None = None
    pdf_status = "not_attempted"
    pdf_pages: int | None = None
    try:
        _ensure_graphiti_runtime(second)
        try:
            merged = await second.api.merge_case(CASE_ID)
            write = getattr(merged, "write", None)
            restart_merge_added = len(getattr(write, "added_keys", ()) or ())
            restart_merge_skipped = len(getattr(write, "skipped_keys", ()) or ())
            registry_readback = restart_merge_skipped > 0
        except Exception as error:
            failures.append(
                {
                    "stage": "restart_merge",
                    "error_type": _error_type(error),
                }
            )
        for name, cutoff in CUTOFFS:
            result: dict[str, Any] = {"name": name, "as_of": cutoff.isoformat()}
            try:
                timeline = await second.graph.timeline(CASE_ID, cutoff)
                result.update(_timeline_summary(timeline, cutoff))
                await second.api.query_historical_snapshot(CASE_ID, cutoff)
            except Exception as error:
                result["error_type"] = _error_type(error)
                result["future_leak_count"] = None
                result["entry_counts"] = {
                    "case": 0,
                    "node": 0,
                    "fact": 0,
                    "claim": 0,
                    "relation": 0,
                    "material": 0,
                }
                failures.append(
                    {"stage": f"cutoff:{name}", "error_type": _error_type(error)}
                )
            cutoff_results.append(result)
        try:
            version = await second.api.save_report_version(
                CASE_ID,
                CUTOFFS[-1][1],
                use_llm=options.report_llm,
                trigger="initial",
            )
            report_saved = True
            report_version_id = getattr(version, "version_id", None)
            report_markdown_chars = len(getattr(version, "markdown", "") or "")
            report_summary_origin = getattr(version, "summary_origin", None)
        except Exception as error:
            failures.append(
                {"stage": "report_version", "error_type": _error_type(error)}
            )
        if report_saved:
            exporter = pdf_exporter or second.api.export_report_pdf
            try:
                export = await _await_result(
                    exporter(report_version_id, "report.pdf")
                )
                pdf_status = "exported"
                pdf_pages = getattr(export, "page_count", None)
                if pdf_pages is None:
                    pdf_pages = getattr(export, "pages", None)
            except Exception as error:
                pdf_status = "unavailable"
                failures.append(
                    {
                        "stage": "pdf",
                        "error_type": _error_type(error),
                        "reason": _pdf_unavailable_reason(error),
                    }
                )
    finally:
        await second.close()

    if materials["failed"]:
        mechanism_status = "fail" if materials["failed"] == materials["total"] else "partial"
    if not registry_readback:
        mechanism_status = "fail"
    if failures and mechanism_status == "pass":
        mechanism_status = "partial"

    overall_status = "fail"
    if mechanism_status != "fail" and semantic_status != "fail" and registry_readback:
        overall_status = (
            "pass"
            if mechanism_status == "pass" and semantic_status == "pass"
            else "partial"
        )

    substantive = quality.get("substantive") if isinstance(quality.get("substantive"), Mapping) else {}
    gaps = quality.get("evidence_gaps") if isinstance(quality.get("evidence_gaps"), Mapping) else {}
    coverage = quality.get("coverage") if isinstance(quality.get("coverage"), Mapping) else {}
    distributions = quality.get("distributions") if isinstance(quality.get("distributions"), Mapping) else {}
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall_status": overall_status,
        "verdict": {
            "mechanism_status": mechanism_status,
            "semantic_status": semantic_status,
            "reasons": list(verdict.get("reasons") or ()),
        },
        "case_id": CASE_ID,
        "prompt_profile": profile_label,
        "graph_backend": "GraphitiBackend",
        "graphiti": {
            "database": options.graphiti_database,
            "group_id": options.graphiti_group_id,
            "loopback_only": True,
            "credentials_source": "environment",
            "model_clients": {
                "llm": options.llm_model,
                "llm_provider": options.llm_provider_name,
                "embedder": "local-deterministic",
                "reranker": "local-deterministic",
            },
        },
        "provider": {
            "name": options.llm_provider_name,
            "model": options.llm_model,
            "credentials_source": "environment",
            "real_calls": options.provider != "synthetic",
        },
        "materials": materials,
        "pipeline": {
            "stage_counts": materials["stage_counts"],
            "dispatch_errors": materials["dispatch_errors"],
        },
        "extract": {
            "target_case_declared": True,
            "target_case_id": CASE_ID,
            "provider": options.llm_provider_name,
            "model": options.llm_model,
        },
        "substantive": substantive,
        "distributions": distributions,
        "coverage": coverage,
        "gaps": gaps,
        "graph_write": {
            "first_pass_added": sum(
                item.get("graph_added", 0) for item in materials["records"]
            ),
            "first_pass_skipped": sum(
                item.get("graph_skipped", 0) for item in materials["records"]
            ),
            "restart_merge_added": restart_merge_added,
            "restart_merge_skipped": restart_merge_skipped,
        },
        "restart": {
            "fresh_runtime": True,
            "runtime_count": 2,
            "registry_readback": registry_readback,
        },
        "cutoffs": cutoff_results,
        "report": {
            "version_saved": report_saved,
            "version_id": report_version_id,
            "markdown_chars": report_markdown_chars,
            "summary_origin": report_summary_origin,
            "llm_used": options.report_llm,
            "pdf_status": pdf_status,
            "pdf_pages": pdf_pages,
        },
        "failures": failures,
    }

    forbidden = {
        str(options.output_dir),
        str(options.output_dir.resolve()),
        options.output_dir.resolve().as_posix(),
    }
    forbidden.update(str(item.path) for item in options.material_files)
    forbidden.update(item.path.resolve().as_posix() for item in options.material_files)
    if overall_status in {"pass", "partial"}:
        # Prompt-profile experiment bridge: a strictly sanitized projection
        # of THIS run's own ledger and quality results, written before the
        # public summary so a sanitization failure can never publish a
        # summary of a "passing" run.
        bridge = build_prompt_run_summary(
            profile=profile_label,
            run_id=run_id,
            case_id=CASE_ID,
            extractions=read_case_extractions(home, CASE_ID),
            quality=quality,
        )
        guard_public_summary(bridge, forbidden_fragments=forbidden)
        _write_json(options.output_dir / BRIDGE_FILENAME, bridge)
    write_public_artifacts(summary, options.output_dir, forbidden_fragments=forbidden)
    return summary


def _provider_from_config(path: Path, provider_name: str) -> tuple[str, str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        provider = raw["llm"]["providers"][provider_name]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise AcceptanceInputError("LLM provider config is invalid") from error
    try:
        return (
            str(provider["api_key_env"]),
            str(provider["base_url"]),
            str(provider["model"]),
        )
    except KeyError as error:
        raise AcceptanceInputError("LLM provider config is incomplete") from error


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Run the real Graphiti/provider acceptance for the narrow Beijing policy case.",
    )
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--llm-config")
    parser.add_argument("--llm-provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--llm-api-key-env", default=DEFAULT_PROVIDER_API_KEY_ENV)
    parser.add_argument("--llm-base-url", default=DEFAULT_PROVIDER_BASE_URL)
    parser.add_argument("--llm-model", default=DEFAULT_PROVIDER_MODEL)
    parser.add_argument("--graphiti-uri", default=os.environ.get("PRISM_GRAPHITI_URI", DEFAULT_BOLT_URI))
    parser.add_argument("--http-uri", default=DEFAULT_HTTP_URI)
    parser.add_argument("--graphiti-password-env", default=DEFAULT_PASSWORD_ENV)
    parser.add_argument("--graphiti-model-max-tokens", type=int, default=4096)
    parser.add_argument("--prompt-profile", default=PROMPT_PROFILE_BASELINE)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--report-llm", action="store_true")
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def _blocked_summary(
    reasons: Sequence[str],
    checks: Mapping[str, bool] | None = None,
    *,
    prompt_profile: str = PROMPT_PROFILE_BASELINE,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall_status": "blocked",
        "verdict": {
            "mechanism_status": "blocked",
            "semantic_status": "not_run",
            "reasons": list(reasons),
        },
        "case_id": CASE_ID,
        "prompt_profile": prompt_profile,
        "graph_backend": "not_connected",
        "preflight": {"status": "blocked", "checks": dict(checks or {})},
        "materials": {
            "total": MATERIAL_COUNT,
            "processed": 0,
            "committed": 0,
            "reused": 0,
            "failed": 0,
            "records": [],
            "stage_counts": {},
            "dispatch_errors": 0,
        },
        "substantive": {},
        "distributions": {},
        "coverage": {},
        "gaps": {},
        "graph_write": {},
        "restart": {
            "fresh_runtime": False,
            "runtime_count": 0,
            "registry_readback": False,
        },
        "cutoffs": [],
        "report": {
            "version_saved": False,
            "version_id": None,
            "markdown_chars": 0,
            "summary_origin": None,
            "llm_used": False,
            "pdf_status": "not_attempted",
            "pdf_pages": None,
        },
        "failures": [{"stage": "preflight", "error_type": "blocked"}],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    output_dir = Path(args.output_dir)
    try:
        # Fail closed on profile/run-id selections before any filesystem or
        # network work: a typo can never silently run the baseline.
        profile_label = resolve_prompt_profile(args.prompt_profile)
        if args.run_id is not None:
            _require_label("run id", args.run_id)
        if args.llm_config:
            api_key_env, base_url, model = _provider_from_config(
                Path(args.llm_config), args.llm_provider
            )
        else:
            api_key_env, base_url, model = (
                args.llm_api_key_env,
                args.llm_base_url,
                args.llm_model,
            )
        material_files = collect_material_files(args.source_root)
        options = RunOptions(
            material_files=material_files,
            output_dir=output_dir,
            llm_provider_name=args.llm_provider,
            llm_api_key_env=api_key_env,
            llm_base_url=base_url,
            llm_model=model,
            bolt_uri=args.graphiti_uri,
            http_uri=args.http_uri,
            graphiti_password_env=args.graphiti_password_env,
            provider=args.llm_provider,
            graphiti_model_max_tokens=args.graphiti_model_max_tokens,
            prompt_profile=profile_label,
            run_id=args.run_id,
            report_llm=args.report_llm,
            strict=args.strict,
        )
        summary = asyncio.run(run_acceptance(options))
    except AcceptanceBlockedError as error:
        summary = _blocked_summary(
            [str(error)],
            checks=getattr(error, "checks", None),
            prompt_profile=profile_label,
        )
        try:
            write_public_artifacts(summary, output_dir)
        except (OSError, SanitizationError):
            pass
        print(f"{TOOL_NAME}: BLOCKED ({'; '.join(summary['verdict']['reasons'])})", file=sys.stderr)
        return 2
    except AcceptanceInputError as error:
        print(f"{TOOL_NAME}: input-error ({type(error).__name__})", file=sys.stderr)
        return 2
    except SanitizationError:
        print(f"{TOOL_NAME}: sanitization-error", file=sys.stderr)
        return 3
    except Exception as error:
        print(f"{TOOL_NAME}: runtime-error ({type(error).__name__})", file=sys.stderr)
        return 1

    print(
        f"{TOOL_NAME}: overall={summary['overall_status']} "
        f"mechanism={summary['verdict']['mechanism_status']} "
        f"semantic={summary['verdict']['semantic_status']} "
        f"profile={summary['prompt_profile']}",
        file=sys.stderr,
    )
    if args.strict and summary["overall_status"] != "pass":
        return 1
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(main())
