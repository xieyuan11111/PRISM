#!/usr/bin/env python3
"""Offline-first prompt-profile experiment runner for the extraction service.

The tool evaluates prompt profiles for ``ExtractionService`` ONLY, on the
normal PRISM composition root with the offline graph backend
(:class:`~prism.runtime.OfflineGraphBackend`); Graphiti/Neo4j is never
started and never imported.  LLM traffic flows through exactly one path —
the official-SDK :class:`~prism.llm.OpenAISDKTransport` wired into the
runtime's :class:`~prism.llm.LLMRouter` — never a bespoke protocol.

Two modes:

* default (no ``--execute``): a pure offline plan/validation pass.  It
  resolves the profile/run id, collects material fingerprints and checks
  readiness (API key env presence, optional SDK installability via
  ``find_spec`` — no import, no network, no runtime) and writes a
  sanitized ``run-plan.json``.
* ``--execute``: the explicit opt-in for REAL provider calls.  It composes
  a fresh output-local runtime, processes every corpus material through
  the automatic pipeline (index → extract → merged-case graph write on the
  offline backend), then projects THIS run's own SQLite ledger plus
  quality-gate results into a strictly sanitized
  ``prompt-run-summary.json`` that ``prism_prompt_benchmark.py`` reads
  directly.  ``--sdk-stream`` switches the SDK transport to its streaming
  ``chat.completions.create`` mode.

Sanitization is inherited from the live acceptance runner's bridge
(``run_live_case_acceptance.build_prompt_run_summary`` +
``guard_public_summary``): the summary carries ids, closed-vocabulary
types, gap-type counts, coverage rates and verdict statuses — never
material bodies, quotes, prompts, corpus or absolute paths, or secrets.

Usage:

    python tools/run_prompt_profile_experiment.py \
        --source-root <material workspace> \
        --output-dir <experiment output directory> \
        --llm-api-key-env <ENV NAME> --llm-base-url <url> \
        --llm-model <model> [--llm-provider primary] \
        [--prompt-profile baseline|protocol-v1] [--run-id <safe-label>] \
        [--sdk-stream] [--execute]
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

import run_live_case_acceptance as acceptance
from prism.config import LLMConfig, LLMProviderConfig, PrismConfig
from prism.domain import EvolutionCase
from prism.llm import OpenAISDKTransport
from prism.extraction import SplitExtractionService
from prism.runtime import OfflineGraphBackend, create_runtime

SCHEMA_VERSION = 1
TOOL_NAME = "prism-prompt-profile-experiment"

CASE_ID = "prompt-experiment-case"
CASE_TYPE = "policy"
CASE_NAME = "Prompt profile experiment case"
CASE_START_AT = datetime(2025, 12, 24, tzinfo=timezone.utc)

BRIDGE_FILENAME = "prompt-run-summary.json"
PLAN_FILENAME = "run-plan.json"

DEFAULT_PROVIDER_NAME = "primary"
DEFAULT_API_KEY_ENV = "PRISM_LLM_API_KEY"
DEFAULT_TIMEOUT = 120.0


class ExperimentError(Exception):
    """Base class for classified experiment failures."""

    kind = "experiment"


class ExperimentBlockedError(ExperimentError):
    """Execution prerequisites are missing; nothing ran."""

    kind = "blocked"


@dataclass(frozen=True, slots=True)
class ExperimentOptions:
    material_files: tuple[acceptance.MaterialFile, ...]
    output_dir: Path
    llm_provider_name: str
    llm_api_key_env: str
    llm_base_url: str
    llm_model: str
    prompt_profile: str
    run_id: str
    sdk_stream: bool = False
    sdk_json_mode: bool = False
    execute: bool = False
    split_v1: bool = False
    case_id: str = CASE_ID
    case_type: str = CASE_TYPE
    case_name: str = CASE_NAME
    case_start_at: datetime = CASE_START_AT
    case_status: str = "active"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    path.write_text(rendered + "\n", encoding="utf-8")


def collect_experiment_materials(
    source_root: str | os.PathLike[str],
) -> tuple[acceptance.MaterialFile, ...]:
    """Collect every corpus Markdown file, represented without its body.

    Material ids are the file stems when they are already safe opaque
    labels; anything else gets a deterministic content-hash id, so no
    private path or prose ever becomes an identifier.
    """

    corpus = Path(source_root) / "corpus"
    if not corpus.is_dir():
        raise acceptance.AcceptanceInputError(
            "source root does not contain a corpus directory"
        )
    files = sorted(corpus.glob("*.md"))
    if not files:
        raise acceptance.AcceptanceInputError("corpus contains no material files")
    collected: list[acceptance.MaterialFile] = []
    for path in files:
        digest = acceptance.sha256_file(path)
        stem = path.stem
        material_id = (
            stem
            if acceptance._SAFE_LABEL.fullmatch(stem)
            else f"mat-{digest[:12]}"
        )
        collected.append(
            acceptance.MaterialFile(
                material_id=material_id,
                path=path.resolve(),
                sha256=digest,
                size_bytes=path.stat().st_size,
            )
        )
    return tuple(collected)


def _build_config(options: ExperimentOptions) -> PrismConfig:
    """An extraction-only, fully offline LLM configuration.

    Only the ``extract`` task role is routed: the experiment evaluates
    ``ExtractionService`` and never involves debate, report or adjudication
    LLM roles.  Graphiti stays disabled (the runtime composes
    ``OfflineGraphBackend``).
    """

    return PrismConfig(
        llm=LLMConfig(
            providers={
                options.llm_provider_name: LLMProviderConfig(
                    model=options.llm_model,
                    api_key_env=options.llm_api_key_env,
                    base_url=options.llm_base_url,
                    timeout=DEFAULT_TIMEOUT,
                    concurrency_limit=1,
                )
            },
            task_roles={"extract": options.llm_provider_name},
        )
    )


def readiness(options: ExperimentOptions) -> tuple[bool, list[str]]:
    """Cheap readiness probes: env presence and find_spec, never imports."""

    reasons: list[str] = []
    if not os.environ.get(options.llm_api_key_env):
        reasons.append(f"{options.llm_api_key_env} is not set")
    if importlib.util.find_spec("openai") is None:
        reasons.append(
            'optional openai SDK is not installed (pip install "news-prism[openai-sdk]")'
        )
    return (not reasons, reasons)


def build_plan(options: ExperimentOptions) -> dict[str, Any]:
    ready, reasons = readiness(options)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "execute": options.execute,
        "profile": options.prompt_profile,
        "run_id": options.run_id,
        "case_id": options.case_id,
        "case_type": options.case_type,
        "case_name": options.case_name,
        "case_start_at": options.case_start_at.isoformat(),
        "case_status": options.case_status,
        "provider": options.llm_provider_name,
        "model": options.llm_model,
        "graph_backend": "offline",
        "sdk_stream": options.sdk_stream,
        "sdk_json_mode": options.sdk_json_mode,
        "split_v1": options.split_v1,
        "materials": len(options.material_files),
        "material_ids": [item.material_id for item in options.material_files],
        "ready": ready,
        "reasons": reasons,
    }


def _build_transport(options: ExperimentOptions) -> Any:
    # The one LLM path this tool ever uses: the official-SDK transport,
    # shared with the runtime default, with streaming as the only option.
    return OpenAISDKTransport(
        stream=options.sdk_stream, json_mode=options.sdk_json_mode
    )


def _install_split_v1(runtime: Any, *, prompt_profile: str | None) -> None:
    """Use the explicit split extractor only for this experiment runtime."""

    router = runtime.llm_router
    if router is None:
        raise ExperimentError("split-v1 requires the LLM router")
    if runtime.adjudicator is not None:
        raise ExperimentError(
            "split-v1 requires an extraction-only experiment configuration"
        )
    split = SplitExtractionService(
        router,
        evidence_locator=runtime.evidence_store.locate,
        prompt_profile=prompt_profile,
    )
    # Experiment-local override after normal composition; the default
    # runtime composition and production pipeline remain unchanged.
    runtime.extraction_service = split
    runtime.pipeline_service._extraction = split
    runtime.api._extraction = split


def _split_failure_stage(error: BaseException) -> str | None:
    """Find a closed split-stage label in a wrapped pipeline error."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        stage = getattr(current, "stage", None)
        if stage in {"stage_a", "stage_b"}:
            return stage
        current = current.__cause__ or current.__context__
    return None


async def _process_materials(
    runtime: Any,
    materials: tuple[acceptance.MaterialFile, ...],
    *,
    case: EvolutionCase,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for item in materials:
        outcome = runtime.pipeline.outcome_for(item.material_id)
        if getattr(outcome, "status", None) == "committed":
            records.append(
                {"material_id": item.material_id, "status": "reused"}
            )
            continue
        try:
            result = await runtime.api.process_material(
                item.path,
                # ``source``/``published_at`` are required by ingestion; the
                # safe-label material id keeps them path-free and the fixed
                # case start keeps them deterministic across reruns.
                {
                    "source": item.material_id,
                    "published_at": case.start_at.isoformat(),
                    "case_tags": [case.case_id],
                },
                target_case=case,
            )
        except Exception as error:  # one material never aborts the run
            records.append(
                {
                    "material_id": item.material_id,
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "failure_stage": _split_failure_stage(error),
                }
            )
            continue
        pipeline_status = getattr(getattr(result, "pipeline", None), "status", None)
        records.append(
            {
                "material_id": item.material_id,
                "status": "committed" if pipeline_status == "completed" else "failed",
                "error_type": None,
            }
        )
    return {
        "total": len(records),
        "processed": sum(
            item["status"] in {"committed", "reused"} for item in records
        ),
        "failed": sum(item["status"] == "failed" for item in records),
        "records": records,
    }


async def run_experiment(
    options: ExperimentOptions,
    *,
    quality_gate: Any | None = None,
) -> dict[str, Any]:
    """Execute the real-provider extraction run and return its bridge.

    Everything stays output-local: the PRISM home (config, SQLite ledgers)
    lives under ``<output-dir>/prism-home`` and only sanitized artifacts
    are written to the output directory itself.
    """

    ready, reasons = readiness(options)
    if not ready:
        raise ExperimentBlockedError("; ".join(reasons))

    output_dir = options.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    home = output_dir / "prism-home"
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.json"
    _build_config(options).save(config_path)
    # Anchor runtime path resolution to the output-local home, like the
    # live acceptance runner, so no default ~/.prism state is touched.
    os.environ["PRISM_HOME"] = str(home)

    transport = _build_transport(options)
    previous_home = os.environ.get("PRISM_HOME")
    os.environ["PRISM_HOME"] = str(home)
    try:
        runtime = await create_runtime(
            config_path,
            llm_transport=transport,
            prompt_profile=options.prompt_profile,
        )
        try:
            if not isinstance(runtime.graph_backend, OfflineGraphBackend):
                raise ExperimentError(
                    "the prompt experiment requires the offline graph backend; "
                    "Graphiti must stay disabled"
                )
            if options.split_v1:
                _install_split_v1(
                    runtime, prompt_profile=options.prompt_profile
                )
            materials = await _process_materials(
                runtime,
                options.material_files,
                case=EvolutionCase(
                    options.case_id,
                    options.case_type,
                    options.case_name,
                    options.case_start_at,
                    options.case_status,
                ),
            )
        finally:
            await runtime.close()
    finally:
        # Never leak the output-local home into the caller's environment.
        if previous_home is None:
            os.environ.pop("PRISM_HOME", None)
        else:
            os.environ["PRISM_HOME"] = previous_home

    if quality_gate is None:
        quality = acceptance._default_quality_gate(
            home, materials, case_id=options.case_id
        )
    else:
        quality = quality_gate(home, materials)
    if hasattr(quality, "__await__"):
        quality = await quality
    if not isinstance(quality, Mapping):
        raise ExperimentError("quality gate must return a JSON object")

    bridge_extractions = list(
        acceptance.read_case_extractions(home, options.case_id)
    )
    failed_gaps: list[dict[str, str]] = []
    for record in materials.get("records", ()):
        if not isinstance(record, Mapping) or record.get("status") != "failed":
            continue
        stage = record.get("failure_stage")
        gap_type = (
            f"{stage}_failure"
            if stage in {"stage_a", "stage_b"}
            else "pipeline_failure"
        )
        failed_gaps.append({"gap_type": gap_type})
    if failed_gaps:
        # A failed extraction has no ledger row. Preserve only the closed
        # failure stage/count in the sanitized benchmark input — never its
        # error message, prompt, material, or path.
        bridge_extractions.append({"evidence_gaps": failed_gaps})
    bridge = acceptance.build_prompt_run_summary(
        profile=options.prompt_profile,
        run_id=options.run_id,
        case_id=options.case_id,
        extractions=tuple(bridge_extractions),
        quality=quality,
    )
    forbidden = {
        str(output_dir),
        str(output_dir.resolve()),
        output_dir.resolve().as_posix(),
    }
    forbidden.update(str(item.path) for item in options.material_files)
    forbidden.update(
        item.path.resolve().as_posix() for item in options.material_files
    )
    acceptance.guard_public_summary(bridge, forbidden_fragments=forbidden)
    _write_json(output_dir / BRIDGE_FILENAME, bridge)
    return bridge


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Evaluate extraction prompt profiles over the offline "
        "runtime through the official-SDK LLM transport.",
    )
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--llm-provider", default=DEFAULT_PROVIDER_NAME)
    parser.add_argument("--llm-api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--llm-base-url", required=True)
    parser.add_argument("--llm-model", required=True)
    parser.add_argument("--case-id", default=CASE_ID)
    parser.add_argument("--case-type", default=CASE_TYPE,
                        choices=("policy", "academic_discourse", "public_issue"))
    parser.add_argument("--case-name", default=CASE_NAME)
    parser.add_argument("--case-start-at", default=CASE_START_AT.isoformat())
    parser.add_argument("--case-status", default="active")
    parser.add_argument("--prompt-profile", default="baseline")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--sdk-stream", action="store_true")
    parser.add_argument(
        "--sdk-json-mode",
        action="store_true",
        help="request OpenAI-compatible JSON object mode through the official SDK",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="explicit opt-in for REAL provider calls (default: offline plan)",
    )
    parser.add_argument(
        "--split-v1",
        action="store_true",
        help="experimental Flash-only two-stage extraction structure",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_dir = Path(args.output_dir)
    try:
        # Fail closed on selections before any filesystem or network work.
        profile_label = acceptance.resolve_prompt_profile(args.prompt_profile)
        run_id = acceptance.resolve_run_id(args.run_id)
        material_files = collect_experiment_materials(args.source_root)
        case_id = acceptance._require_label("case id", args.case_id)
        if not isinstance(args.case_name, str) or not args.case_name.strip():
            raise acceptance.AcceptanceInputError("case name must be non-empty")
        try:
            case_start_at = datetime.fromisoformat(args.case_start_at)
        except ValueError as error:
            raise acceptance.AcceptanceInputError(
                "case start must be an ISO 8601 datetime"
            ) from error
        if case_start_at.tzinfo is None or case_start_at.utcoffset() is None:
            raise acceptance.AcceptanceInputError(
                "case start must be timezone-aware"
            )
        options = ExperimentOptions(
            material_files=material_files,
            output_dir=output_dir,
            llm_provider_name=acceptance._require_label(
                "LLM provider name", args.llm_provider
            ),
            llm_api_key_env=acceptance._require_label(
                "LLM API variable name", args.llm_api_key_env
            ),
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            prompt_profile=profile_label,
            run_id=run_id,
            sdk_stream=args.sdk_stream,
            sdk_json_mode=args.sdk_json_mode,
            execute=args.execute,
            split_v1=args.split_v1,
            case_id=case_id,
            case_type=args.case_type,
            case_name=args.case_name.strip(),
            case_start_at=case_start_at,
            case_status=args.case_status,
        )

        if not args.execute:
            plan = build_plan(options)
            acceptance.guard_public_summary(plan)
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_json(output_dir / PLAN_FILENAME, plan)
            print(
                f"{TOOL_NAME}: plan ready={plan['ready']} "
                f"materials={plan['materials']} profile={plan['profile']} "
                f"stream={'on' if plan['sdk_stream'] else 'off'} "
                "(dry run; pass --execute for real calls)",
                file=sys.stderr,
            )
            return 0

        bridge = asyncio.run(run_experiment(options))
        print(
            f"{TOOL_NAME}: executed profile={bridge['profile']} "
            f"run_id={bridge['run_id']} case={bridge['case_id']} "
            f"nodes={len(bridge['candidates']['node']['ids'])} "
            f"gaps={sum(bridge['gap_types'].values())}",
            file=sys.stderr,
        )
        return 0
    except acceptance.AcceptanceInputError as error:
        print(f"{TOOL_NAME}: input-error ({error})", file=sys.stderr)
        return 2
    except ExperimentBlockedError as error:
        print(f"{TOOL_NAME}: blocked ({error})", file=sys.stderr)
        return 2
    except acceptance.SanitizationError:
        print(f"{TOOL_NAME}: sanitization-error", file=sys.stderr)
        return 3
    except ExperimentError as error:
        print(f"{TOOL_NAME}: experiment-error ({error})", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"{TOOL_NAME}: runtime-error ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
