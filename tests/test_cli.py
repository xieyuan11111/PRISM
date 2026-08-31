"""Focused contract tests for the dependency-free PRISM CLI (module 8)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path

from prism.cli import (
    build_parser,
    handle_ingest,
    handle_search,
    handle_timeline,
    main,
)


NOW = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc)


def run_cli(argv, api):
    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(main(argv, api=api, stdout=stdout, stderr=stderr))
    return status, stdout.getvalue(), stderr.getvalue()


class FakeAPI:
    def __init__(self):
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def search(self, query, **filters):
        self.calls.append(("search", (query,), filters))
        return [
            {
                "title": "Evidence",
                "published_at": NOW,
                "path": Path("corpus/evidence.md"),
                "case_tags": ("case-1",),
            }
        ]

    async def build_timeline(self, case_id, as_of):
        self.calls.append(("build_timeline", (case_id, as_of), {}))
        return {"entries": (), "as_of": as_of, "case_id": case_id}

    async def ingest_material(self, path, metadata=None):
        self.calls.append(("ingest_material", (path, metadata), {}))
        return {
            "material_id": "material-1",
            "metadata": metadata,
            "api_key": "must-not-leak",
        }


def test_build_parser_exposes_three_small_subcommands_and_public_handlers():
    parser = build_parser()

    search = parser.parse_args(["search", "housing"])
    timeline = parser.parse_args(
        ["timeline", "case-1", "--as-of", "2026-09-01T09:30:00+00:00"]
    )
    ingest = parser.parse_args(["ingest", "input.md"])

    assert search.handler is handle_search
    assert timeline.handler is handle_timeline
    assert ingest.handler is handle_ingest


def test_search_delegates_every_filter_to_injected_api_and_prints_stable_json():
    api = FakeAPI()

    status, stdout, stderr = run_cli(
        [
            "search",
            "housing policy",
            "--case-tag",
            "case-1",
            "--source",
            "example.gov",
            "--type",
            "policy",
            "--after",
            "2026-08-01T00:00:00Z",
            "--before",
            "2026-09-01T00:00:00+08:00",
            "--limit",
            "7",
            "--offset",
            "2",
        ],
        api,
    )

    assert status == 0
    assert stderr == ""
    assert api.calls == [
        (
            "search",
            ("housing policy",),
            {
                "case_tag": "case-1",
                "source": "example.gov",
                "type": "policy",
                "published_after": datetime(
                    2026, 8, 1, tzinfo=timezone.utc
                ),
                "published_before": datetime.fromisoformat(
                    "2026-09-01T00:00:00+08:00"
                ),
                "limit": 7,
                "offset": 2,
            },
        )
    ]
    assert stdout == (
        '[{"case_tags":["case-1"],"path":"corpus/evidence.md",'
        '"published_at":"2026-09-01T09:30:00+00:00","title":"Evidence"}]\n'
    )


def test_search_defaults_are_explicit_at_the_api_boundary():
    api = FakeAPI()

    status, _, _ = run_cli(["search", "housing"], api)

    assert status == 0
    assert api.calls == [
        (
            "search",
            ("housing",),
            {
                "case_tag": None,
                "source": None,
                "type": None,
                "published_after": None,
                "published_before": None,
                "limit": 50,
                "offset": 0,
            },
        )
    ]


def test_timeline_requires_and_delegates_an_aware_iso_timestamp():
    api = FakeAPI()

    status, stdout, stderr = run_cli(
        ["timeline", "case-1", "--as-of", "2026-09-01T17:30:00+08:00"],
        api,
    )

    expected = datetime.fromisoformat("2026-09-01T17:30:00+08:00")
    assert status == 0
    assert stderr == ""
    assert api.calls == [("build_timeline", ("case-1", expected), {})]
    assert json.loads(stdout) == {
        "as_of": "2026-09-01T17:30:00+08:00",
        "case_id": "case-1",
        "entries": [],
    }


def test_naive_timeline_timestamp_is_a_usage_error_and_does_not_call_api():
    api = FakeAPI()

    status, stdout, stderr = run_cli(
        ["timeline", "case-1", "--as-of", "2026-09-01T09:30:00"], api
    )

    assert status == 2
    assert stdout == ""
    assert "timezone-aware" in stderr
    assert api.calls == []


def test_ingest_parses_json_object_metadata_and_redacts_sensitive_keys():
    api = FakeAPI()

    status, stdout, stderr = run_cli(
        [
            "ingest",
            "input.md",
            "--metadata",
            '{"source":"example.gov","token":"metadata-secret"}',
        ],
        api,
    )

    metadata = {"source": "example.gov", "token": "metadata-secret"}
    assert status == 0
    assert stderr == ""
    assert api.calls == [("ingest_material", (Path("input.md"), metadata), {})]
    assert "metadata-secret" not in stdout
    assert "must-not-leak" not in stdout
    assert json.loads(stdout) == {
        "api_key": "[REDACTED]",
        "material_id": "material-1",
        "metadata": {"source": "example.gov", "token": "[REDACTED]"},
    }


def test_ingest_rejects_non_object_json_before_calling_api():
    api = FakeAPI()

    status, stdout, stderr = run_cli(
        ["ingest", "input.md", "--metadata", '["not", "an", "object"]'], api
    )

    assert status == 2
    assert stdout == ""
    assert "JSON object" in stderr
    assert api.calls == []


def test_invalid_limit_is_a_usage_error_before_calling_api():
    api = FakeAPI()

    status, stdout, stderr = run_cli(
        ["search", "housing", "--limit", "0"], api
    )

    assert status == 2
    assert stdout == ""
    assert "positive integer" in stderr
    assert api.calls == []


def test_runtime_errors_go_only_to_stderr_with_nonzero_status_and_redaction():
    class FailingAPI(FakeAPI):
        async def search(self, query, **filters):
            raise RuntimeError("provider token=runtime-secret failed")

    status, stdout, stderr = run_cli(["search", "housing"], FailingAPI())

    assert status == 1
    assert stdout == ""
    assert "RuntimeError" in stderr
    assert "runtime-secret" not in stderr
    assert "[REDACTED]" in stderr


def test_search_evidence_is_supported_as_facade_compatibility_name():
    class SearchEvidenceAPI:
        def __init__(self):
            self.calls = []

        async def search_evidence(self, query, **filters):
            self.calls.append((query, filters))
            return []

    api = SearchEvidenceAPI()

    status, stdout, stderr = run_cli(["search", "housing"], api)

    assert status == 0
    assert stdout == "[]\n"
    assert stderr == ""
    assert len(api.calls) == 1
