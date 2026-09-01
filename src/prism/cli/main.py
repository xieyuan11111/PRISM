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

    async def ingest_material(
        self, path: str | Path, metadata: dict[str, Any] | None = None
    ) -> object: ...

    async def report_case(
        self, case_id: str, as_of: datetime | None = None, use_llm: bool = True
    ) -> object: ...


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

    ingest = commands.add_parser("ingest", help="Ingest a Markdown or PDF file.")
    ingest.add_argument("input_path", type=Path, metavar="INPUT")
    ingest.add_argument("--metadata", type=_json_object, metavar="JSON")
    ingest.set_defaults(handler=handle_ingest)

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


async def handle_ingest(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed ingest command to the injected facade."""
    return await _await_api_call(api.ingest_material(args.input_path, args.metadata))


async def handle_report(args: argparse.Namespace, api: PrismAPIProtocol) -> object:
    """Delegate a parsed report command to the injected facade."""
    return await _await_api_call(
        api.report_case(args.case_id, args.as_of, use_llm=not args.no_llm)
    )


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
            await owned_runtime.close()
            owned_runtime = None
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
    "handle_ingest",
    "handle_report",
    "handle_search",
    "handle_timeline",
    "main",
]
