"""Offline contracts for the real-case live Graphiti acceptance runner.

These tests never start Neo4j/Graphiti, never call DeepSeek, and never read a
real material.  They inject fakes at the runner's seams and verify the safety
and reproducibility contract that the opt-in live execution must preserve.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from prism.config import GraphitiConfig, PrismConfig
from prism.graph import GraphitiBackend
from graphiti_fakes import FakeGraphitiClient

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import run_live_case_acceptance as runner  # noqa: E402


UTC = timezone.utc


class FakePortProbe:
    def __init__(self, open_ports: set[tuple[str, int]]) -> None:
        self.open_ports = open_ports
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int, *, timeout: float) -> bool:
        self.calls.append((host, port))
        return (host, port) in self.open_ports


class FakeAPI:
    def __init__(self, timeline_entries: tuple[Any, ...] = ()) -> None:
        self.timeline_entries = timeline_entries
        self.process_calls: list[dict[str, Any]] = []
        self.merge_calls: list[str] = []
        self.snapshot_calls: list[tuple[str, str]] = []
        self.report_calls: list[tuple[str, str]] = []

    async def process_material(
        self,
        source: str | Path,
        metadata: dict[str, Any] | None = None,
        *,
        target_case: Any = None,
    ) -> Any:
        self.process_calls.append(
            {
                "source": str(source),
                "metadata": dict(metadata or {}),
                "target_case_id": getattr(target_case, "case_id", None),
            }
        )
        return SimpleNamespace(
            pipeline=SimpleNamespace(status="completed"),
            case_outcome=SimpleNamespace(
                case_id=target_case.case_id,
                bundle=SimpleNamespace(
                    case=target_case,
                    nodes=(SimpleNamespace(),),
                    temporal_facts=(SimpleNamespace(),),
                    claims=(),
                    relations=(),
                    materials=(SimpleNamespace(),),
                ),
                write=SimpleNamespace(added_keys=("episode",), skipped_keys=()),
                material_ids=("material",),
                warnings=(),
            ),
            warnings=(),
        )

    async def merge_case(self, case_id: str) -> Any:
        self.merge_calls.append(case_id)
        return SimpleNamespace(
            case_id=case_id,
            bundle=SimpleNamespace(
                case=SimpleNamespace(case_id=case_id),
                nodes=(),
                temporal_facts=(),
                claims=(),
                relations=(),
                materials=(),
            ),
            write=SimpleNamespace(added_keys=(), skipped_keys=("episode",)),
            material_ids=(),
            warnings=(),
        )

    async def query_historical_snapshot(
        self, case_id: str, as_of: datetime, **kwargs: Any
    ) -> Any:
        self.snapshot_calls.append((case_id, as_of.isoformat()))
        return SimpleNamespace(entries=self.timeline_entries, invalidated_entries=())

    async def save_report_version(
        self, case_id: str, as_of: datetime | None = None, **kwargs: Any
    ) -> Any:
        self.report_calls.append((case_id, as_of.isoformat() if as_of else None))
        return SimpleNamespace(version_id="rv_synthetic", markdown="report")


class FakeRuntime:
    def __init__(self, api: FakeAPI, config: PrismConfig) -> None:
        self.api = api
        self.config = config
        self.graph_backend = GraphitiBackend(
            FakeGraphitiClient(), group_id="neo4j"
        )
        self.graph = SimpleNamespace(
            timeline=self._timeline,
        )
        self.pipeline = SimpleNamespace(outcome_for=lambda material_id: None)
        self.dispatch_errors = ()
        self.closed = False

    async def _timeline(self, case_id: str, as_of: datetime) -> Any:
        return SimpleNamespace(entries=self.api.timeline_entries, invalidated_entries=())

    async def close(self) -> None:
        self.closed = True


def _material_files(root: Path) -> list[runner.MaterialFile]:
    corpus = root / "corpus"
    corpus.mkdir(parents=True)
    files: list[runner.MaterialFile] = []
    for index, material_id in enumerate(runner.MATERIAL_IDS):
        path = corpus / f"material-{index}-{material_id}.md"
        path.write_text(f"synthetic body {index}\n", encoding="utf-8")
        files.append(
            runner.MaterialFile(
                material_id=material_id,
                path=path,
                sha256=runner.sha256_file(path),
                size_bytes=path.stat().st_size,
            )
        )
    return files


def test_collects_exactly_the_four_narrow_beijing_materials(tmp_path: Path) -> None:
    expected = _material_files(tmp_path)
    collected = runner.collect_material_files(tmp_path)

    assert [item.material_id for item in collected] == list(runner.MATERIAL_IDS)
    assert [str(item.path) for item in collected] == [str(item.path) for item in expected]
    assert all(item.sha256 and item.size_bytes > 0 for item in collected)

    with pytest.raises(runner.AcceptanceInputError, match="material file not found"):
        runner.collect_material_files(tmp_path, material_ids=("mat_missing",))


def test_preflight_requires_loopback_prism_ports_and_environment_credentials() -> None:
    probe = FakePortProbe({("127.0.0.1", 7475), ("127.0.0.1", 7688)})
    result = runner.preflight_live_services(
        bolt_uri="bolt://127.0.0.1:7688",
        http_uri="http://127.0.0.1:7475",
        graphiti_password_env="PRISM_GRAPHITI_PASSWORD",
        provider_api_key_env="DEEPSEEK_API_KEY",
        probe=probe,
        environ={
            "PRISM_GRAPHITI_PASSWORD": "value-not-inspected",
            "DEEPSEEK_API_KEY": "value-not-inspected",
        },
    )
    assert result.status == "ready"
    assert probe.calls == [("127.0.0.1", 7475), ("127.0.0.1", 7688)]
    assert result.checks == {
        "bolt_loopback": True,
        "http_loopback": True,
        "graphiti_password_env": True,
        "provider_api_key_env": True,
    }

    missing_env = runner.preflight_live_services(
        "bolt://127.0.0.1:7688",
        "http://127.0.0.1:7475",
        "PRISM_GRAPHITI_PASSWORD",
        "DEEPSEEK_API_KEY",
        probe=FakePortProbe(set()),
        environ={},
    # Credentials must not suppress TCP preflight results.
    )
    assert missing_env.status == "blocked"
    assert list(missing_env.reasons) == [
        "PRISM_GRAPHITI_PASSWORD is not set",
        "DEEPSEEK_API_KEY is not set",
        "Graphiti HTTP endpoint is not listening on loopback",
        "Graphiti Bolt endpoint is not listening on loopback",
    ]
    assert missing_env.checks == {
        "bolt_loopback": False,
        "http_loopback": False,
        "graphiti_password_env": False,
        "provider_api_key_env": False,
    }

    closed_http = runner.preflight_live_services(
        "bolt://127.0.0.1:7688",
        "http://127.0.0.1:7475",
        "PRISM_GRAPHITI_PASSWORD",
        "DEEPSEEK_API_KEY",
        probe=FakePortProbe({("127.0.0.1", 7688)}),
        environ={
            "PRISM_GRAPHITI_PASSWORD": "value",
            "DEEPSEEK_API_KEY": "value",
        },
    )
    assert closed_http.status == "blocked"
    assert list(closed_http.reasons) == ["Graphiti HTTP endpoint is not listening on loopback"]

    remote_probe = FakePortProbe(set())
    remote = runner.preflight_live_services(
        "bolt://192.0.2.10:7688",
        "http://192.0.2.10:7475",
        "PRISM_GRAPHITI_PASSWORD",
        "DEEPSEEK_API_KEY",
        probe=remote_probe,
        environ={
            "PRISM_GRAPHITI_PASSWORD": "value",
            "DEEPSEEK_API_KEY": "value",
        },
    )
    assert remote.status == "blocked"
    assert list(remote.reasons) == ["Graphiti Bolt URI host must be loopback"]
    assert remote_probe.calls == []

    default_port = runner.preflight_live_services(
        "bolt://127.0.0.1:7687",
        "http://127.0.0.1:7475",
        "PRISM_GRAPHITI_PASSWORD",
        "DEEPSEEK_API_KEY",
        probe=FakePortProbe(set()),
        environ={
            "PRISM_GRAPHITI_PASSWORD": "value",
            "DEEPSEEK_API_KEY": "value",
        },
    )
    assert default_port.status == "blocked"
    assert "Graphiti Bolt URI port must be 7688" in default_port.reasons


def test_blocked_summary_reports_preflight_without_claiming_connection() -> None:
    summary = runner._blocked_summary(
        ("required environment variables are not set",),
        checks={
            "bolt_loopback": False,
            "http_loopback": False,
            "graphiti_password_env": False,
            "provider_api_key_env": False,
        },
    )

    assert summary["overall_status"] == "blocked"
    assert summary["graph_backend"] == "not_connected"
    assert summary["preflight"]["checks"] == {
        "bolt_loopback": False,
        "http_loopback": False,
        "graphiti_password_env": False,
        "provider_api_key_env": False,
    }


def test_runner_uses_target_case_real_graphiti_restart_and_two_cutoffs(
    tmp_path: Path,
) -> None:
    material_files = _material_files(tmp_path)
    output_dir = tmp_path / "public"
    options = runner.RunOptions(
        material_files=material_files,
        output_dir=output_dir,
        llm_provider_name="deepseek",
        llm_api_key_env="DEEPSEEK_API_KEY",
        llm_base_url="https://api.deepseek.com/v1",
        llm_model="deepseek-chat",
        bolt_uri="bolt://127.0.0.1:7688",
        http_uri="http://127.0.0.1:7475",
        graphiti_password_env="PRISM_GRAPHITI_PASSWORD",
        provider="synthetic",
    )

    first_api = FakeAPI()
    second_api = FakeAPI(
        timeline_entries=(
            SimpleNamespace(
                kind="evolution_node",
                source_ids=("mat_1",),
                evidence=(SimpleNamespace(),),
                reference_time=datetime(2026, 8, 1, tzinfo=UTC),
                valid_at=datetime(2026, 8, 1, tzinfo=UTC),
                invalid_at=None,
            ),
        )
    )
    config = PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri=options.bolt_uri,
            database="neo4j",
            group_id="neo4j",
            password_env=options.graphiti_password_env,
        )
    )
    first = FakeRuntime(first_api, config)
    first.dispatch_errors = (SimpleNamespace(), SimpleNamespace())
    second = FakeRuntime(second_api, config)
    runtimes = [first, second]
    configs: list[PrismConfig] = []

    def runtime_factory(config_path: Path) -> FakeRuntime:
        configs.append(runner.PrismConfig.load(config_path))
        return runtimes.pop(0)

    quality_report = {
        "verdict": {
            "mechanism_status": "pass",
            "semantic_status": "partial",
            "reasons": ["synthetic reason"],
        },
        "substantive": {"total": 2},
        "evidence_gaps": {"total": 1},
        "coverage": {
            "source_ids": {"rate": 1.0},
            "evidence_locator": {"rate": 1.0},
        },
    }

    summary = asyncio.run(
        runner.run_acceptance(
            options,
            runtime_factory=runtime_factory,
            preflight=lambda: runner.PreflightResult(
                status="ready", reasons=(), checks={}
            ),
            quality_gate=lambda home, summary: quality_report,
            pdf_exporter=lambda version_id, output_path: SimpleNamespace(
                pages=1, output_path=output_path
            ),
        )
    )

    assert len(first_api.process_calls) == 4
    assert {
        call["target_case_id"] for call in first_api.process_calls
    } == {runner.CASE_ID}
    assert all(
        call["metadata"]["case_tags"] == [runner.CASE_ID]
        for call in first_api.process_calls
    )
    assert first.closed and second.closed
    assert second_api.merge_calls == [runner.CASE_ID]
    assert [name for name, _ in second_api.snapshot_calls] == [
        runner.CASE_ID,
        runner.CASE_ID,
    ]
    assert second_api.report_calls == [(runner.CASE_ID, runner.CUTOFFS[-1][1].isoformat())]
    assert [item.graphiti.enabled for item in configs] == [True, True]
    assert [item.graphiti.database for item in configs] == ["neo4j", "neo4j"]

    assert summary["case_id"] == runner.CASE_ID
    assert summary["materials"]["total"] == 4
    assert summary["materials"]["processed"] == 4
    assert summary["pipeline"]["dispatch_errors"] == 2
    assert summary["graph_backend"] == "GraphitiBackend"
    assert summary["restart"]["fresh_runtime"] is True
    assert summary["restart"]["registry_readback"] is True
    assert [item["name"] for item in summary["cutoffs"]] == [
        "before-materials",
        "after-materials",
    ]
    assert all(item["future_leak_count"] == 0 for item in summary["cutoffs"])
    assert summary["report"]["version_saved"] is True
    assert summary["report"]["pdf_status"] == "exported"

    public_json = output_dir / "acceptance-summary.json"
    public_md = output_dir / "acceptance-summary.md"
    assert public_json.is_file() and public_md.is_file()
    text = public_json.read_text(encoding="utf-8") + public_md.read_text(encoding="utf-8")
    assert "D:\\Hermas" not in text
    assert "E:\\Projects" not in text
    assert not re.search(r"[A-Za-z]:[/\\]", text)
    assert not re.search(r"(?i)\b(api[_-]?key|password|secret|token)\b", text)
    assert json.loads(public_json.read_text(encoding="utf-8")) == summary


def test_public_summary_rejects_private_paths_and_secret_wording() -> None:
    leaked = {
        "path": "D:/private/material.md",
        "secret": "api_key=secret-value",
        "nested": ["C:/Users/somebody/report.pdf"],
    }
    with pytest.raises(runner.SanitizationError):
        runner.guard_public_summary(leaked, forbidden_fragments=("private/material.md",))
