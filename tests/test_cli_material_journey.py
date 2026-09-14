"""Contract tests for the CLI material-journey commands and the ``prism`` entry.

Covers docs/cli-ai-callable-requirements.md C-1 (console-script entry) and
C-3 (material-journeys / material-journey): single-line JSON on stdout,
JSON errors on stderr, exit codes 0/1/2, honest empty results, the reused
seven-step journey projection, and never leaking pipeline stage payloads.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace

from prism.api.facade import MaterialJourneyView
from prism.cli import (
    build_parser,
    handle_material_journey,
    handle_material_journeys,
    main,
    main_entry,
)


NOW = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(hours=1)


def run_cli(argv, api):
    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(main(argv, api=api, stdout=stdout, stderr=stderr))
    return status, stdout.getvalue(), stderr.getvalue()


def _committed_view(material_id="mat-1", occurred_at=EARLIER):
    return MaterialJourneyView(
        material_id=material_id,
        display_name="Housing memo",
        source_format="md",
        case_id="case-1",
        raw_path="raw/2026/09/mat-1.md",
        corpus_path="corpus/2026/09/mat-1.md",
        content_hash="abc123",
        fetched_at=occurred_at,
        occurred_at=occurred_at,
        lifecycle_status="committed",
        run=SimpleNamespace(
            material_id=material_id,
            status="completed",
            detail=None,
            correlation_id="corr-1",
            started_at=occurred_at,
            finished_at=occurred_at,
            stages=(
                SimpleNamespace(
                    name="index",
                    status="indexed",
                    detail=None,
                    result="FULL DOCUMENT CONTENT MUST NOT LEAK",
                ),
                SimpleNamespace(
                    name="extract",
                    status="extracted",
                    detail=None,
                    result="MORE SECRET PAYLOAD",
                ),
                SimpleNamespace(
                    name="graph",
                    status="written",
                    detail=None,
                    result=None,
                ),
            ),
        ),
        mechanism_status="pass",
        evidence_gap_count=0,
        report_version_id="rv-1",
    )


def _failed_view(material_id="mat-2", occurred_at=NOW):
    return MaterialJourneyView(
        material_id=material_id,
        display_name="Broken pdf",
        source_format="pdf",
        case_id=None,
        lifecycle_status="failed",
        occurred_at=occurred_at,
        run=SimpleNamespace(
            material_id=material_id,
            status="failed",
            detail=None,
            correlation_id="corr-2",
            started_at=occurred_at,
            finished_at=occurred_at,
            stages=(
                SimpleNamespace(name="index", status="indexed", detail=None),
                SimpleNamespace(
                    name="extract", status="extracted", detail=None
                ),
            ),
        ),
        failure=SimpleNamespace(
            stage="extract",
            error_type="ExtractionError",
            message="extraction failed for document",
            failed_at=occurred_at,
        ),
        mechanism_status="fail",
        report_version_id=None,
    )


class JourneyFakeAPI:
    """Fake facade returning real MaterialJourneyView objects."""

    def __init__(self, listing=(), single=None, single_error=None):
        self.listing = tuple(listing)
        self.single = single
        self.single_error = single_error
        self.journeys_calls: list[dict] = []
        self.journey_calls: list[str] = []

    async def material_journeys(self, *, case_id=None, status=None):
        self.journeys_calls.append({"case_id": case_id, "status": status})
        return self.listing

    async def material_journey(self, material_id):
        self.journey_calls.append(material_id)
        if self.single_error is not None:
            raise self.single_error
        return self.single


# ----------------------------------------------------------- the prism entry


def test_pyproject_declares_the_prism_console_script():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["prism"] == "prism.cli:main_entry"


def test_main_entry_prints_the_same_help_as_the_module_call(monkeypatch, capsys):
    module_buffer = StringIO()
    module_status = asyncio.run(
        main(["--help"], stdout=module_buffer, stderr=module_buffer)
    )
    module_help = capsys.readouterr().out
    assert module_status == 0
    assert "material-journeys" in module_help

    monkeypatch.setattr(sys, "argv", ["prism", "--help"])
    entry_status = main_entry()
    entry_help = capsys.readouterr().out

    assert entry_status == 0
    assert entry_help == module_help


def test_main_entry_returns_the_usage_error_exit_status(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prism", "material-journey"])
    status = main_entry()
    captured = capsys.readouterr()

    assert status == 2
    error = json.loads(captured.err)
    assert error["error"]["type"] == "usage"


# --------------------------------------------------- material-journeys (list)


def test_parser_registers_the_material_journey_subcommands():
    parser = build_parser()

    listed = parser.parse_args(["material-journeys"])
    assert listed.handler is handle_material_journeys
    assert listed.case_id is None
    assert listed.status is None

    single = parser.parse_args(["material-journey", "mat-1"])
    assert single.handler is handle_material_journey
    assert single.material_id == "mat-1"


def test_material_journeys_lists_json_rows_in_facade_order():
    api = JourneyFakeAPI(
        listing=(_failed_view(), _committed_view())
    )

    status, out, err = run_cli(["material-journeys"], api)

    assert status == 0
    assert err == ""
    assert out.endswith("\n") and out.count("\n") == 1
    rows = json.loads(out)
    assert [row["material_id"] for row in rows] == ["mat-2", "mat-1"]
    failed, committed = rows
    assert failed["display_name"] == "Broken pdf"
    assert failed["lifecycle_status"] == "failed"
    assert failed["occurred_at"] == "2026-09-01T09:30:00+00:00"
    assert failed["failed_stage"] == "extract"
    assert failed["error_type"] == "ExtractionError"
    assert committed["display_name"] == "Housing memo"
    assert committed["lifecycle_status"] == "committed"
    assert committed["occurred_at"] == "2026-09-01T08:30:00+00:00"
    assert committed["report_version_id"] == "rv-1"
    assert api.journeys_calls == [{"case_id": None, "status": None}]


def test_material_journeys_forwards_case_and_status_filters():
    api = JourneyFakeAPI(listing=(_committed_view(occurred_at=NOW),))

    status, out, err = run_cli(
        ["material-journeys", "--case-id", "case-1", "--status", "committed"],
        api,
    )

    assert status == 0
    assert err == ""
    assert api.journeys_calls == [
        {"case_id": "case-1", "status": "committed"}
    ]
    assert [row["material_id"] for row in json.loads(out)] == ["mat-1"]


def test_material_journeys_empty_result_is_an_empty_json_array_with_success():
    api = JourneyFakeAPI(listing=())

    status, out, err = run_cli(["material-journeys"], api)

    assert status == 0
    assert err == ""
    assert out == "[]\n"


def test_material_journeys_rejects_unknown_status_without_calling_api():
    api = JourneyFakeAPI()

    status, out, err = run_cli(
        ["material-journeys", "--status", "archived"], api
    )

    assert status == 2
    assert out == ""
    error = json.loads(err)
    assert error["error"]["type"] == "usage"
    assert api.journeys_calls == []


def test_material_journeys_rejects_blank_case_id_without_calling_api():
    api = JourneyFakeAPI()

    status, out, err = run_cli(["material-journeys", "--case-id", " "], api)

    assert status == 2
    assert out == ""
    assert json.loads(err)["error"]["type"] == "usage"
    assert api.journeys_calls == []


# --------------------------------------------------- material-journey (one)


def test_material_journey_returns_seven_steps_run_and_quality():
    api = JourneyFakeAPI(single=_committed_view(occurred_at=NOW))

    status, out, err = run_cli(["material-journey", "mat-1"], api)

    assert status == 0
    assert err == ""
    assert api.journey_calls == ["mat-1"]
    payload = json.loads(out)
    assert payload["material_id"] == "mat-1"
    assert payload["case_id"] == "case-1"
    assert payload["lifecycle_status"] == "committed"
    assert payload["occurred_at"] == "2026-09-01T09:30:00+00:00"
    assert [step["step"] for step in payload["steps"]] == [
        "staged",
        "ingested",
        "indexed",
        "extracted",
        "merged",
        "graph_written",
        "analyzed",
    ]
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["stages"][0]["name"] == "index"
    assert "result" not in payload["run"]["stages"][0]
    assert "FULL DOCUMENT CONTENT" not in out
    assert payload["failure"] is None
    assert payload["mechanism_status"] == "pass"
    assert payload["semantic_status"] == "unknown"
    assert payload["evidence_gap_count"] == 0
    steps = {step["step"]: step for step in payload["steps"]}
    assert steps["ingested"]["status"] == "completed"
    assert steps["merged"]["status"] == "completed"
    assert steps["analyzed"]["status"] == "completed"


def test_material_journey_marks_the_failed_step_and_carries_failure_detail():
    api = JourneyFakeAPI(single=_failed_view())

    status, out, err = run_cli(["material-journey", "mat-2"], api)

    assert status == 0
    assert err == ""
    payload = json.loads(out)
    assert payload["lifecycle_status"] == "failed"
    assert payload["failure"] == {
        "stage": "extract",
        "error_type": "ExtractionError",
        "message": "extraction failed for document",
        "failed_at": "2026-09-01T09:30:00+00:00",
    }
    steps = {step["step"]: step for step in payload["steps"]}
    assert steps["extracted"]["status"] == "failed"
    assert steps["extracted"]["time"] == "2026-09-01T09:30:00+00:00"
    assert payload["mechanism_status"] == "fail"
    assert payload["report_version_id"] is None


def test_material_journey_unknown_material_is_a_json_error_with_status_1():
    api = JourneyFakeAPI(single_error=LookupError("material not found: nope"))

    status, out, err = run_cli(["material-journey", "nope"], api)

    assert status == 1
    assert out == ""
    error = json.loads(err)
    assert error["error"]["type"] == "LookupError"
    assert "nope" in error["error"]["message"]


def test_material_journey_rejects_blank_material_id():
    api = JourneyFakeAPI()

    status, out, err = run_cli(["material-journey", "  "], api)

    assert status == 2
    assert out == ""
    assert json.loads(err)["error"]["type"] == "usage"
    assert api.journey_calls == []


# --------------------------------------------------------- output purity


def test_both_commands_emit_single_line_json_without_ansi():
    listing_api = JourneyFakeAPI(
        listing=(_failed_view(), _committed_view())
    )
    single_api = JourneyFakeAPI(single=_failed_view())

    for argv, api in (
        (["material-journeys"], listing_api),
        (["material-journey", "mat-2"], single_api),
    ):
        status, out, err = run_cli(argv, api)
        assert status == 0
        assert err == ""
        assert "\x1b" not in out
        assert out.endswith("\n") and out.count("\n") == 1
        json.loads(out)
