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


DEFAULT_SEARCH_LIMIT = 50


class PrismAPIProtocol(Protocol):
    """The facade operations used by this shell.

    ``search`` is intentionally resolved at runtime because older facade
    versions used the more explicit ``search_evidence`` name.
    """

    async def search(self, query: str, **filters: Any) -> object: ...

    async def build_timeline(self, case_id: str, as_of: datetime) -> object: ...

    async def query_case_state(self, case_id: str, cutoff_at: datetime) -> object: ...

    async def ingest_material(
        self, path: str | Path, metadata: dict[str, Any] | None = None
    ) -> object: ...

    async def process_material(
        self, source: str, metadata: dict[str, Any] | None = None
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

    async def fetch_source(
        self, url: str, *, kind: str = "auto", process: bool = True
    ) -> object: ...

    async def fetch_sources(
        self, urls: Sequence[str], *, kind: str = "auto", process: bool = True
    ) -> object: ...

    async def plan_research_by_id(self, source_id: str) -> object: ...

    async def execute_research(self, plan: object, *, process: bool = True) -> object: ...


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
    report.set_defaults(handler=handle_report)

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


async def handle_ingest(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed ingest command to the injected facade."""
    if args.process:
        return await _await_api_call(
            api.process_material(args.input_path, args.metadata)
        )
    return await _await_api_call(api.ingest_material(args.input_path, args.metadata))


async def handle_process(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed process command to the injected facade."""
    return await _await_api_call(api.process_material(args.source, args.metadata))


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
    return await _await_api_call(
        api.report_case(args.case_id, args.as_of, use_llm=not args.no_llm)
    )


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
    "handle_search",
    "handle_state",
    "handle_timeline",
    "main",
]
