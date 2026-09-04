"""Dependency-free argparse shell over the PRISM application facade."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime
import inspect
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Protocol, TextIO

from prism.analyzer import ENTRY_KINDS, STAGES
from prism.domain import EvolutionCase


DEFAULT_SEARCH_LIMIT = 50


class PrismAPIProtocol(Protocol):
    """The facade operations used by this shell.

    ``search`` is intentionally resolved at runtime because older facade
    versions used the more explicit ``search_evidence`` name.
    """

    async def search(self, query: str, **filters: Any) -> object: ...

    async def build_timeline(self, case_id: str, as_of: datetime) -> object: ...

    async def query_case_state(self, case_id: str, cutoff_at: datetime) -> object: ...

    async def query_historical_snapshot(
        self,
        case_id: str,
        as_of: datetime,
        *,
        stage: str | None = None,
        kinds: Sequence[str] | None = None,
    ) -> object: ...

    async def compare_case_history(
        self,
        case_id: str,
        earlier: datetime,
        later: datetime,
        *,
        kinds: Sequence[str] | None = None,
    ) -> object: ...

    async def ingest_material(
        self, path: str | Path, metadata: dict[str, Any] | None = None
    ) -> object: ...

    async def process_material(
        self,
        source: str,
        metadata: dict[str, Any] | None = None,
        target_case: object | None = None,
    ) -> object: ...

    async def merge_case(
        self, case_id: str, materials: Sequence[str] | None = None
    ) -> object: ...

    async def bind_material_to_case(
        self, material_id: str, case_id: str
    ) -> object: ...

    async def report_case(
        self, case_id: str, as_of: datetime | None = None, use_llm: bool = True
    ) -> object: ...

    async def save_report_version(
        self,
        case_id: str,
        as_of: datetime | None = None,
        use_llm: bool = True,
        debate_result: object | None = None,
        trigger: str = "initial",
    ) -> object: ...

    async def report_versions(
        self, case_id: str | None = None, *, as_of: datetime | None = None
    ) -> object: ...

    async def report_version(self, version_id: str) -> object: ...

    async def export_report_pdf(
        self, version_id: str, output_path: str | Path
    ) -> object: ...

    async def add_material(
        self,
        source: str,
        target_case: object,
        metadata: dict[str, Any] | None = None,
        as_of: datetime | None = None,
        use_llm: bool = True,
    ) -> object: ...

    async def rebuild_report(
        self, case_id: str, as_of: datetime | None = None, use_llm: bool = True
    ) -> object: ...

    async def case_overviews(self, **filters: object) -> object: ...

    async def debate_case(
        self,
        case_id: str,
        question: str,
        as_of: datetime | None = None,
        perspectives: Sequence[str] | None = None,
    ) -> object: ...

    async def follow_up_debate(
        self, parent_run_id: str, question: str, perspective: str
    ) -> object: ...

    async def fetch_source(
        self, url: str, *, kind: str = "auto", process: bool = True
    ) -> object: ...

    async def fetch_sources(
        self, urls: Sequence[str], *, kind: str = "auto", process: bool = True
    ) -> object: ...

    async def plan_research_by_id(self, source_id: str) -> object: ...

    async def execute_research(self, plan: object, *, process: bool = True) -> object: ...

    def adjudication_history(self, material_id: str | None = None) -> object: ...


class _ParserExit(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _ArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose exits can be converted into async return codes."""

    def error(self, message: str) -> None:
        raise _ParserExit(2, message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise _ParserExit(status, message or "")


def _nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must be a non-empty string")
    return value


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _nonnegative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a non-negative integer"
        ) from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _aware_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO-8601 timezone-aware timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "must be an ISO-8601 timezone-aware timestamp"
        )
    return parsed


def _perspective_ids(value: str) -> list[str]:
    ids = [part.strip() for part in value.split(",") if part.strip()]
    if not ids:
        raise argparse.ArgumentTypeError(
            "must be a comma-separated list of perspective ids"
        )
    if len(set(ids)) != len(ids):
        raise argparse.ArgumentTypeError("must not repeat perspective ids")
    return ids


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            f"must be a valid JSON object: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _case_from_json(value: dict[str, Any]) -> EvolutionCase:
    """Build one caller-declared EvolutionCase from parsed ``--case-json``.

    The mapping mirrors the domain model's fields; timestamps must be
    timezone-aware ISO 8601 strings.  Invalid shapes fail with an explicit
    error before any API call.
    """
    required = {"case_id", "case_type", "canonical_name", "start_at", "status"}
    allowed = required | {"node_ids", "status_at", "status_observed_at"}
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(
            "--case-json missing required field(s): " + ", ".join(missing)
        )
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError(
            "--case-json contains unexpected field(s): " + ", ".join(extra)
        )
    node_ids = value.get("node_ids") or []
    if not isinstance(node_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in node_ids
    ):
        raise ValueError(
            "--case-json field node_ids must be a JSON array of non-empty strings"
        )
    start_at = value["start_at"]
    if not isinstance(start_at, str):
        raise ValueError(
            "--case-json field start_at must be an ISO-8601 "
            "timezone-aware timestamp"
        )

    def timestamp(name: str) -> datetime | None:
        item = value.get(name)
        if item is None:
            return None
        if not isinstance(item, str):
            raise ValueError(
                f"--case-json field {name} must be an ISO-8601 "
                "timezone-aware timestamp"
            )
        return _aware_datetime(item)

    try:
        return EvolutionCase(
            case_id=value["case_id"],
            case_type=value["case_type"],
            canonical_name=value["canonical_name"],
            start_at=_aware_datetime(start_at),
            status=value["status"],
            node_ids=tuple(node_ids),
            status_at=timestamp("status_at"),
            status_observed_at=timestamp("status_observed_at"),
        )
    except (TypeError, ValueError, argparse.ArgumentTypeError) as error:
        raise ValueError(f"--case-json is not a valid evolution case: {error}") from error


def build_parser() -> argparse.ArgumentParser:
    """Build and return the public PRISM command-line parser."""
    parser = _ArgumentParser(prog="prism", description="Query and ingest PRISM data.")
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="Search indexed evidence.")
    search.add_argument("query", type=_nonempty)
    search.add_argument("--case-tag", type=_nonempty)
    search.add_argument("--source", type=_nonempty)
    search.add_argument("--type", type=_nonempty)
    search.add_argument(
        "--after",
        "--published-after",
        dest="published_after",
        metavar="TIMESTAMP",
        type=_aware_datetime,
    )
    search.add_argument(
        "--before",
        "--published-before",
        dest="published_before",
        metavar="TIMESTAMP",
        type=_aware_datetime,
    )
    search.add_argument("--limit", type=_positive_integer, default=DEFAULT_SEARCH_LIMIT)
    search.add_argument("--offset", type=_nonnegative_integer, default=0)
    search.set_defaults(handler=handle_search)

    cases = commands.add_parser(
        "cases", help="List accumulated evolution cases from the local ledger."
    )
    cases.add_argument("case_id", nargs="?", type=_nonempty, metavar="CASE_ID")
    cases.add_argument("--type", type=_nonempty, metavar="TYPE")
    cases.add_argument("--status", type=_nonempty, metavar="STATUS")
    cases.add_argument("--unresolved-only", action="store_true")
    cases.add_argument(
        "--order",
        choices=("case_id", "last_updated", "latest_observed"),
        default="case_id",
    )
    cases.add_argument("--reverse", action="store_true")
    cases.set_defaults(handler=handle_cases)

    timeline = commands.add_parser("timeline", help="Query a case timeline.")
    timeline.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    timeline.add_argument(
        "--as-of",
        required=True,
        type=_aware_datetime,
        metavar="TIMESTAMP",
        help="ISO-8601 timestamp with a UTC offset.",
    )
    timeline.set_defaults(handler=handle_timeline)

    state = commands.add_parser(
        "state", help="Query auditable case state at a historical cutoff."
    )
    state.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    state.add_argument(
        "--cutoff-at",
        required=True,
        type=_aware_datetime,
        metavar="TIMESTAMP",
        help="ISO-8601 timestamp with a UTC offset.",
    )
    state.set_defaults(handler=handle_state)

    snapshot = commands.add_parser(
        "snapshot",
        help=(
            "Return the formal GTI-backed historical snapshot at an instant: "
            "effective nodes, facts, claims, relations, facts invalidated by "
            "the cutoff and evidence gaps."
        ),
    )
    snapshot.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    snapshot.add_argument(
        "--as-of",
        required=True,
        type=_aware_datetime,
        metavar="TIMESTAMP",
        help="ISO-8601 timestamp with a UTC offset.",
    )
    snapshot.add_argument(
        "--stage",
        choices=sorted(STAGES),
        metavar="STAGE",
        help=(
            "Restrict the snapshot to one deterministic recorded stage, e.g. "
            "publication (policy chain) or support (discourse claim stance). "
            "Allowed: "
            + ", ".join(sorted(STAGES))
            + "."
        ),
    )
    snapshot.add_argument(
        "--kind",
        action="append",
        choices=sorted(ENTRY_KINDS),
        metavar="KIND",
        help=(
            "Restrict the snapshot to one entry kind; repeatable. Allowed: "
            + ", ".join(sorted(ENTRY_KINDS))
            + "."
        ),
    )
    snapshot.set_defaults(handler=handle_snapshot)

    compare = commands.add_parser(
        "compare",
        help=(
            "Compare one case's effective state at two historical instants, "
            "returning added/removed/unchanged entries with their layers."
        ),
    )
    compare.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    compare.add_argument(
        "--earlier",
        required=True,
        type=_aware_datetime,
        metavar="TIMESTAMP",
        help="Earlier ISO-8601 timestamp with a UTC offset.",
    )
    compare.add_argument(
        "--later",
        required=True,
        type=_aware_datetime,
        metavar="TIMESTAMP",
        help="Later ISO-8601 timestamp with a UTC offset (must not precede --earlier).",
    )
    compare.add_argument(
        "--kind",
        action="append",
        choices=sorted(ENTRY_KINDS),
        metavar="KIND",
        help=(
            "Restrict the comparison to one entry kind; repeatable. Allowed: "
            + ", ".join(sorted(ENTRY_KINDS))
            + "."
        ),
    )
    compare.set_defaults(handler=handle_compare)

    ingest = commands.add_parser(
        "ingest", help="Ingest a Markdown or PDF file and announce it."
    )
    ingest.add_argument("input_path", type=Path, metavar="INPUT")
    ingest.add_argument("--metadata", type=_json_object, metavar="JSON")
    ingest.add_argument(
        "--process",
        dest="process",
        action="store_true",
        help=(
            "Wait for the automatic pipeline and print its full outcome "
            "(index, extract, case merge, graph write).  Without it this "
            "command prints the ingestion result; automatic processing is "
            "still queued and completes before the command exits (a "
            "processing failure makes the command exit non-zero)."
        ),
    )
    ingest.set_defaults(handler=handle_ingest)

    process = commands.add_parser(
        "process",
        help=(
            "Run one material through the automatic pipeline synchronously, "
            "waiting for the pipeline and case outcome before returning."
        ),
    )
    process.add_argument(
        "source",
        type=_nonempty,
        metavar="MATERIAL_OR_INPUT",
        help="An indexed material id, or a path to a Markdown/PDF input.",
    )
    process.add_argument("--metadata", type=_json_object, metavar="JSON")
    process_target = process.add_mutually_exclusive_group()
    process_target.add_argument(
        "--case-id",
        type=_nonempty,
        metavar="CASE_ID",
        help=(
            "Process this material as part of an already recorded evolution "
            "case: the case is loaded from the durable ledger and any "
            "extraction drift or case: null fails the material auditably."
        ),
    )
    process_target.add_argument(
        "--case-json",
        type=_json_object,
        metavar="JSON",
        help=(
            "Declare the target evolution case inline as a JSON object with "
            "case_id, case_type, canonical_name, start_at (timezone-aware "
            "ISO 8601) and status (optionally node_ids, status_at, "
            "status_observed_at)."
        ),
    )
    process.set_defaults(handler=handle_process)

    merge_case = commands.add_parser(
        "merge-case",
        help=(
            "Rebuild and write one case's accumulated extractions from the "
            "durable ledger (idempotent reconciliation, no re-extraction)."
        ),
    )
    merge_case.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    merge_case.add_argument(
        "--materials",
        nargs="+",
        type=_nonempty,
        metavar="MATERIAL_ID",
        help=(
            "Merge only these accumulated materials; omit to rebuild the "
            "full accumulated case."
        ),
    )
    merge_case.set_defaults(handler=handle_merge_case)

    bind_material = commands.add_parser(
        "bind-material",
        help=(
            "Explicitly bind pending material-scoped evidence to an existing "
            "case, then rebuild and write that case."
        ),
    )
    bind_material.add_argument(
        "material_id", type=_nonempty, metavar="MATERIAL_ID"
    )
    bind_material.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    bind_material.set_defaults(handler=handle_bind_material)

    report = commands.add_parser(
        "report", help="Render a case evolution report as JSON."
    )
    report.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    report.add_argument(
        "--as-of",
        required=False,
        type=_aware_datetime,
        metavar="TIMESTAMP",
        help="ISO-8601 timestamp with a UTC offset; defaults to now.",
    )
    report.add_argument(
        "--no-llm",
        dest="no_llm",
        action="store_true",
        help="Disable the LLM summary and render deterministically.",
    )
    report.add_argument(
        "--save",
        dest="save",
        action="store_true",
        help="Persist the rendered report as an immutable version.",
    )
    report.set_defaults(handler=handle_report)

    report_versions = commands.add_parser(
        "report-versions", help="List immutable report versions."
    )
    report_versions.add_argument(
        "case_id", nargs="?", type=_nonempty, metavar="CASE_ID"
    )
    report_versions.add_argument(
        "--as-of",
        type=_aware_datetime,
        metavar="TIMESTAMP",
        help="List only versions rendered for this historical cutoff.",
    )
    report_versions.set_defaults(handler=handle_report_versions)

    report_version = commands.add_parser(
        "report-version", help="Read one immutable report version."
    )
    report_version.add_argument("version_id", type=_nonempty, metavar="VERSION_ID")
    report_version.add_argument(
        "--pdf",
        dest="pdf",
        type=_nonempty,
        metavar="OUTPUT_PDF",
        help="Export this version to a PDF path relative to PRISM output.",
    )
    report_version.set_defaults(handler=handle_report_version)

    report_pdf = commands.add_parser(
        "report-pdf", help="Export one immutable report version as a PDF."
    )
    report_pdf.add_argument("version_id", type=_nonempty, metavar="VERSION_ID")
    report_pdf.add_argument("output_path", type=_nonempty, metavar="OUTPUT_PATH")
    report_pdf.set_defaults(handler=handle_report_pdf)

    add_material = commands.add_parser(
        "add-material",
        help="Append a material to a known case and recompute its report.",
    )
    add_material.add_argument("source", type=_nonempty, metavar="MATERIAL_OR_INPUT")
    add_material.add_argument("--metadata", type=_json_object, metavar="JSON")
    add_material.add_argument(
        "--case-id", required=True, type=_nonempty, metavar="CASE_ID"
    )
    add_material.add_argument("--as-of", type=_aware_datetime, metavar="TIMESTAMP")
    add_material.add_argument("--no-llm", action="store_true")
    add_material.set_defaults(handler=handle_add_material)

    rebuild_report = commands.add_parser(
        "rebuild-report", help="Recompute and version a case report."
    )
    rebuild_report.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    rebuild_report.add_argument("--as-of", type=_aware_datetime, metavar="TIMESTAMP")
    rebuild_report.add_argument("--no-llm", action="store_true")
    rebuild_report.set_defaults(handler=handle_rebuild_report)

    debate = commands.add_parser(
        "debate", help="Run automatic multi-perspective debate for a case."
    )
    debate.add_argument("case_id", type=_nonempty, metavar="CASE_ID")
    debate.add_argument(
        "--question",
        required=True,
        type=_nonempty,
        metavar="TEXT",
        help="Question the perspectives must interpret from recorded evidence.",
    )
    debate.add_argument(
        "--as-of",
        required=False,
        type=_aware_datetime,
        metavar="TIMESTAMP",
        help="ISO-8601 timestamp with a UTC offset; defaults to now.",
    )
    debate.add_argument(
        "--perspectives",
        required=False,
        type=_perspective_ids,
        metavar="A,B",
        help="Optional comma-separated perspective ids; defaults are selected by case type.",
    )
    debate.set_defaults(handler=handle_debate)

    follow_up = commands.add_parser(
        "follow-up", help="Ask one named perspective a follow-up question."
    )
    follow_up.add_argument("parent_run_id", type=_nonempty, metavar="PARENT_RUN_ID")
    follow_up.add_argument("--perspective", required=True, type=_nonempty, metavar="ID")
    follow_up.add_argument("--question", required=True, type=_nonempty, metavar="TEXT")
    follow_up.set_defaults(handler=handle_follow_up)

    adjudication = commands.add_parser(
        "adjudication-history", help="Read LLM automatic-adjudication audit records."
    )
    adjudication.add_argument("material_id", nargs="?", type=_nonempty, metavar="MATERIAL_ID")
    adjudication.set_defaults(handler=handle_adjudication_history)

    fetch = commands.add_parser(
        "fetch", help="Fetch one whitelisted public source URL."
    )
    fetch.add_argument("url", type=_nonempty, metavar="URL")
    fetch.add_argument(
        "--kind",
        type=_nonempty,
        default="auto",
        metavar="KIND",
        help="Source payload kind: auto (default), feed, or page.",
    )
    fetch.add_argument(
        "--no-process",
        dest="no_process",
        action="store_true",
        help="Stop after ingestion and skip the extraction pipeline.",
    )
    fetch.set_defaults(handler=handle_fetch)

    fetch_all = commands.add_parser(
        "fetch-all",
        help="Fetch many whitelisted URLs from a JSON array file or a comma-separated list.",
    )
    fetch_all.add_argument(
        "urls",
        type=_nonempty,
        metavar="URLS",
        help="Comma-separated URLs, or a path to a JSON array of URL strings.",
    )
    fetch_all.add_argument(
        "--kind",
        type=_nonempty,
        default="auto",
        metavar="KIND",
        help="Source payload kind: auto (default), feed, or page.",
    )
    fetch_all.add_argument(
        "--no-process",
        dest="no_process",
        action="store_true",
        help="Stop after ingestion and skip the extraction pipeline.",
    )
    fetch_all.set_defaults(handler=handle_fetch_all)

    discover = commands.add_parser(
        "discover", help="Create a temporal research plan for an indexed material."
    )
    discover.add_argument("source_id", type=_nonempty, metavar="MATERIAL_ID")
    discover.set_defaults(handler=handle_discover)

    research = commands.add_parser(
        "research", help="Search and re-collect evidence for an indexed material."
    )
    research.add_argument("source_id", type=_nonempty, metavar="MATERIAL_ID")
    research.add_argument(
        "--no-process",
        dest="no_process",
        action="store_true",
        help="Stop after authoritative ingestion and skip extraction/graph processing.",
    )
    research.set_defaults(handler=handle_research)
    return parser


async def _await_api_call(call: object) -> object:
    if not inspect.isawaitable(call):
        raise TypeError("PrismAPI command methods must be async")
    return await call


async def handle_search(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed search command to the injected facade."""
    method = getattr(api, "search", None)
    if not callable(method):
        method = getattr(api, "search_evidence", None)
    if not callable(method):
        raise TypeError("PrismAPI must provide search()")
    return await _await_api_call(
        method(
            args.query,
            case_tag=args.case_tag,
            source=args.source,
            type=args.type,
            published_after=args.published_after,
            published_before=args.published_before,
            limit=args.limit,
            offset=args.offset,
        )
    )


async def handle_timeline(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed timeline command to the injected facade."""
    return await _await_api_call(api.build_timeline(args.case_id, args.as_of))


async def handle_state(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Return status, nodes, facts, interpretations and gaps at a cutoff."""

    return await _await_api_call(api.query_case_state(args.case_id, args.cutoff_at))


async def handle_snapshot(
    args: argparse.Namespace, api: PrismAPIProtocol
) -> object:
    """Delegate a parsed snapshot command to the injected facade."""
    kinds = tuple(args.kind) if args.kind else None
    return await _await_api_call(
        api.query_historical_snapshot(
            args.case_id, args.as_of, stage=args.stage, kinds=kinds
        )
    )


async def handle_compare(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed compare command to the injected facade."""
    kinds = tuple(args.kind) if args.kind else None
    return await _await_api_call(
        api.compare_case_history(
            args.case_id, args.earlier, args.later, kinds=kinds
        )
    )


async def handle_ingest(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed ingest command to the injected facade."""
    if args.process:
        return await _await_api_call(
            api.process_material(args.input_path, args.metadata)
        )
    return await _await_api_call(api.ingest_material(args.input_path, args.metadata))


async def handle_process(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed process command to the injected facade."""
    target_case = None
    if args.case_id is not None:
        target_case = args.case_id
    elif args.case_json is not None:
        target_case = _case_from_json(args.case_json)
    if target_case is None:
        return await _await_api_call(api.process_material(args.source, args.metadata))
    return await _await_api_call(
        api.process_material(args.source, args.metadata, target_case=target_case)
    )


async def handle_merge_case(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed merge-case command to the injected facade."""
    return await _await_api_call(
        api.merge_case(args.case_id, materials=args.materials)
    )


async def handle_bind_material(
    args: argparse.Namespace, api: PrismAPIProtocol
) -> object:
    """Bind one explicitly named pending material to an existing case."""
    return await _await_api_call(
        api.bind_material_to_case(args.material_id, args.case_id)
    )


async def handle_report(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed report command to the injected facade."""
    if args.save:
        return await _await_api_call(
            api.save_report_version(
                args.case_id, args.as_of, use_llm=not args.no_llm, trigger="initial"
            )
        )
    return await _await_api_call(
        api.report_case(args.case_id, args.as_of, use_llm=not args.no_llm)
    )

async def handle_cases(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed case-overview command to the injected facade."""
    return await _await_api_call(
        api.case_overviews(
            case_id=args.case_id,
            case_type=args.type,
            status=args.status,
            unresolved_only=args.unresolved_only,
            order=args.order,
            reverse=args.reverse,
        )
    )

async def handle_report_versions(
    args: argparse.Namespace, api: PrismAPIProtocol
) -> object:
    """Delegate report-version listing to the injected facade."""
    return await _await_api_call(
        api.report_versions(args.case_id, as_of=args.as_of)
    )

async def handle_report_version(
    args: argparse.Namespace, api: PrismAPIProtocol
) -> object:
    """Delegate one report-version read to the injected facade."""
    if args.pdf is not None:
        return await _await_api_call(
            api.export_report_pdf(args.version_id, args.pdf)
        )
    return await _await_api_call(api.report_version(args.version_id))


async def handle_report_pdf(
    args: argparse.Namespace, api: PrismAPIProtocol
) -> object:
    """Delegate one report-version PDF export to the facade."""
    return await _await_api_call(
        api.export_report_pdf(args.version_id, args.output_path)
    )

async def handle_add_material(
    args: argparse.Namespace, api: PrismAPIProtocol
) -> object:
    """Append one material and recompute the target case report."""
    return await _await_api_call(
        api.add_material(
            args.source,
            args.case_id,
            args.metadata,
            as_of=args.as_of,
            use_llm=not args.no_llm,
        )
    )

async def handle_rebuild_report(
    args: argparse.Namespace, api: PrismAPIProtocol
) -> object:
    """Delegate an explicit report rebuild to the injected facade."""
    return await _await_api_call(
        api.rebuild_report(
            args.case_id, args.as_of, use_llm=not args.no_llm
        )
    )


async def handle_debate(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate an automatic debate command to the injected facade."""
    return await _await_api_call(
        api.debate_case(
            args.case_id,
            args.question,
            args.as_of,
            perspectives=args.perspectives,
        )
    )


async def handle_follow_up(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate one named-perspective follow-up to the facade."""
    return await _await_api_call(
        api.follow_up_debate(args.parent_run_id, args.question, args.perspective)
    )


async def handle_adjudication_history(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    result = api.adjudication_history(args.material_id)
    return await _await_api_call(result) if inspect.isawaitable(result) else result


def _resolve_url_source(value: str) -> list[str]:
    """Interpret one fetch-all argument: comma-separated URLs or a JSON file."""
    if "://" in value:
        urls = [part.strip() for part in value.split(",") if part.strip()]
        if not urls:
            raise ValueError("URL list is empty")
        return urls
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"URL list file not found: {value}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"URL list file is not valid JSON: {exc.msg}") from exc
    if (
        not isinstance(payload, list)
        or not payload
        or any(not isinstance(item, str) or not item.strip() for item in payload)
    ):
        raise ValueError(
            "URL list file must contain a JSON array of non-empty URL strings"
        )
    return [item.strip() for item in payload]


async def handle_fetch(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed fetch command to the injected facade."""
    return await _await_api_call(
        api.fetch_source(args.url, kind=args.kind, process=not args.no_process)
    )


async def handle_fetch_all(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Resolve the URL list, then delegate to the injected facade."""
    urls = _resolve_url_source(args.urls)
    return await _await_api_call(
        api.fetch_sources(urls, kind=args.kind, process=not args.no_process)
    )


async def handle_research(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Plan then execute research for one indexed material."""
    plan = await _await_api_call(api.plan_research_by_id(args.source_id))
    return await _await_api_call(
        api.execute_research(plan, process=not args.no_process)
    )


async def handle_discover(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Create, but do not execute, a research plan."""
    return await _await_api_call(api.plan_research_by_id(args.source_id))


_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|passwd|secret|token)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|credential|password|passwd|secret|token)"
    r"\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")


def _is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and _SENSITIVE_KEY.search(key) is not None


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, os.PathLike):
        return Path(os.fspath(value)).as_posix()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: (
                "[REDACTED]"
                if _is_sensitive_key(field.name)
                else _jsonable(getattr(value, field.name))
            )
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON output mappings must have string keys")
            result[key] = "[REDACTED]" if _is_sensitive_key(key) else _jsonable(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"unsupported API result type: {type(value).__name__}")


def _write_json(value: object, stream: TextIO) -> None:
    rendered = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    stream.write(rendered + "\n")


def _safe_error_message(error: BaseException) -> str:
    message = str(error)
    message = _SENSITIVE_VALUE.sub(r"\1[REDACTED]", message)
    return _BEARER_VALUE.sub(r"\1[REDACTED]", message)


def _dispatch_error_payload(errors: Sequence[object]) -> dict[str, object]:
    """Render the first isolated subscriber failure as an error payload.

    The audit fields of one failure (subscriber, event type, material id,
    failure time and the underlying error) are surfaced verbatim, so a
    background processing failure never exits 0 as if it had succeeded.
    """
    first = errors[0]
    subscription_id = getattr(first, "subscription_id", "?")
    event = getattr(first, "event", None)
    event_type = getattr(event, "event_type", "?")
    payload = getattr(event, "payload", {})
    material_id = payload.get("material_id") if isinstance(payload, Mapping) else None
    exception = getattr(first, "exception", None)
    detail = f"{type(exception).__name__}: {exception}"
    failed_at = getattr(first, "failed_at", None)
    when = failed_at.isoformat() if isinstance(failed_at, datetime) else "unknown"
    message = (
        f"automatic pipeline subscriber {subscription_id} failed while "
        f"handling {event_type}"
        + (
            f" for material {material_id!r}"
            if isinstance(material_id, str) and material_id.strip()
            else ""
        )
        + f" at {when}: {_safe_error_message(detail)}"
    )
    return {
        "error": {
            "message": message,
            "type": type(first).__name__,
            "count": len(errors),
        }
    }


async def main(
    argv: Sequence[str] | None = None,
    api: PrismAPIProtocol | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Parse, dispatch, and render one command; return a process exit status.

    An injected facade keeps tests and embeddings isolated.  The command-line
    default is an owned, offline-safe runtime that is closed before returning.
    """
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _ParserExit as exc:
        if exc.status == 0:
            if exc.message:
                output.write(exc.message)
            return 0
        _write_json(
            {
                "error": {
                    "message": _safe_error_message(exc),
                    "type": "usage",
                }
            },
            errors,
        )
        return exc.status

    owned_runtime = None
    try:
        if api is None:
            from prism.runtime import create_runtime

            owned_runtime = await create_runtime()
            api = owned_runtime.api
        result = await args.handler(args, api)
        if owned_runtime is not None:
            # Closing the owned runtime drains the event bus, so queued
            # automatic processing has finished by this point.  A subscriber
            # failure must flip the exit status: the command never reports a
            # result as if background processing had succeeded.
            await owned_runtime.close()
            dispatch_failures = owned_runtime.dispatch_errors
            owned_runtime = None
            if dispatch_failures:
                _write_json(_dispatch_error_payload(dispatch_failures), errors)
                return 1
        _write_json(result, output)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _write_json(
            {
                "error": {
                    "message": _safe_error_message(exc),
                    "type": type(exc).__name__,
                }
            },
            errors,
        )
        return 1
    finally:
        if owned_runtime is not None:
            await owned_runtime.close()
    return 0


__all__ = [
    "PrismAPIProtocol",
    "build_parser",
    "handle_bind_material",
    "handle_fetch",
    "handle_fetch_all",
    "handle_discover",
    "handle_merge_case",
    "handle_process",
    "handle_research",
    "handle_ingest",
    "handle_report",
    "handle_report_version",
    "handle_report_pdf",
    "handle_report_versions",
    "handle_add_material",
    "handle_rebuild_report",
    "handle_cases",
    "handle_debate",
    "handle_follow_up",
    "handle_adjudication_history",
    "handle_search",
    "handle_snapshot",
    "handle_compare",
    "handle_state",
    "handle_timeline",
    "main",
]
