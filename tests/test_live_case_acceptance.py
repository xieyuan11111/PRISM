"""Offline contracts for the real-case live Graphiti acceptance runner.

These tests never start Neo4j/Graphiti, never call DeepSeek, and never read a
real material.  They inject fakes at the runner's seams and verify the safety
and reproducibility contract that the opt-in live execution must preserve.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
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

    async def export_report_pdf(self, version_id: str, destination: Path) -> Any:
        destination.write_bytes(b"%PDF-synthetic")
        return SimpleNamespace(page_count=1)


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


def test_process_materials_takes_inputs_explicitly_without_mutating_runtime() -> None:
    """The live runner must not attach ad-hoc state to slots-based runtime."""
    runtime = FakeRuntime(FakeAPI(), PrismConfig())
    materials = tuple(
        runner.MaterialFile(
            material_id=f"mat_{index}",
            path=Path(f"material-{index}.md"),
            sha256="a" * 64,
            size_bytes=1,
        )
        for index in range(4)
    )

    records, _ = asyncio.run(runner._process_materials(runtime, materials))

    assert len(records["records"]) == 4
    assert not hasattr(runtime, "_acceptance_materials")


def test_run_home_is_selected_as_prism_home_before_runtime_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_bridge_ledger(tmp_path / "output")
    options = runner.RunOptions(
        material_files=tuple(_material_files(tmp_path)),
        output_dir=tmp_path / "output",
        llm_provider_name="deepseek",
        llm_api_key_env="DEEPSEEK_API_KEY",
        llm_base_url="https://example.invalid/v1",
        llm_model="test-model",
        bolt_uri="bolt://127.0.0.1:7688",
        http_uri="http://127.0.0.1:7475",
        graphiti_password_env="PRISM_GRAPHITI_PASSWORD",
        provider="deepseek",
    )
    monkeypatch.delenv("PRISM_HOME", raising=False)
    captured: list[Path] = []

    async def runtime_factory(config_path: Path) -> Any:
        captured.append(config_path)
        return FakeRuntime(FakeAPI(), PrismConfig())

    async def quality_gate(home: Path, materials: Any) -> dict[str, Any]:
        return {"verdict": {"mechanism_status": "pass", "semantic_status": "pass"}}

    asyncio.run(
        runner.run_acceptance(
            options,
            runtime_factory=runtime_factory,
            preflight=lambda: runner.PreflightResult("ready", (), {}),
            quality_gate=quality_gate,
        )
    )
    config_path = tmp_path / "output" / "prism-home" / "config.json"
    assert captured == [config_path, config_path]
    assert Path(__import__("os").environ["PRISM_HOME"]) == config_path.parent


def test_pdf_export_receives_relative_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_bridge_ledger(tmp_path / "output")
    options = runner.RunOptions(
        material_files=tuple(_material_files(tmp_path)),
        output_dir=tmp_path / "output",
        llm_provider_name="deepseek",
        llm_api_key_env="DEEPSEEK_API_KEY",
        llm_base_url="https://example.invalid/v1",
        llm_model="test-model",
        bolt_uri="bolt://127.0.0.1:7688",
        http_uri="http://127.0.0.1:7475",
        graphiti_password_env="PRISM_GRAPHITI_PASSWORD",
        provider="deepseek",
    )
    monkeypatch.delenv("PRISM_HOME", raising=False)
    destinations: list[object] = []

    async def runtime_factory(config_path: Path) -> Any:
        return FakeRuntime(FakeAPI(), PrismConfig())

    async def quality_gate(home: Path, materials: Any) -> dict[str, Any]:
        return {"verdict": {"mechanism_status": "pass", "semantic_status": "pass"}}

    async def pdf_exporter(version_id: str, destination: object) -> Any:
        destinations.append(destination)
        return SimpleNamespace(page_count=1)

    asyncio.run(
        runner.run_acceptance(
            options,
            runtime_factory=runtime_factory,
            preflight=lambda: runner.PreflightResult("ready", (), {}),
            quality_gate=quality_gate,
            pdf_exporter=pdf_exporter,
        )
    )
    assert destinations == ["report.pdf"]


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
    _plant_bridge_ledger(tmp_path / "public")
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
                payload=json.dumps({"node_type": "implementation"}),
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
        "distributions": {"node_type": {"implementation": 3}},
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
    assert summary["distributions"]["node_type"] == {"implementation": 3}
    assert summary["cutoffs"][-1]["node_type"] == {"implementation": 1}
    assert summary["graph_write"]["first_pass_added"] == 4
    assert summary["graph_write"]["first_pass_skipped"] == 0
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


# ------------------------------------------------- prompt-profile experiment loop

_BRIDGE_LEDGER_EXTRACTION = {
    "case": {
        "case_id": runner.CASE_ID,
        "case_type": "policy",
        "canonical_name": "Synthetic bridge case",
        "start_at": "2026-01-10T00:00:00+00:00",
        "status": "active",
    },
    "nodes": [
        {
            "id": "policy-2026-proposal",
            "case_id": runner.CASE_ID,
            "node_type": "proposal",
            "happened_at": "2026-01-10T00:00:00+00:00",
            "summary": "synthetic bridge node",
            "source_ids": ["mat_source"],
        }
    ],
    "temporal_facts": [],
    "claims": [],
    "conflicts": [],
    "relations": [],
    "evidence_gaps": [],
    "warnings": [],
}


def _plant_bridge_ledger(output_dir: Path) -> None:
    """Pre-create this run's own home ledger for the bridge projection."""

    data = output_dir / "prism-home" / "data"
    data.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(data / "index.db"))
    connection.executescript(
        "CREATE TABLE IF NOT EXISTS case_extraction_ledger ("
        "case_id TEXT NOT NULL, material_id TEXT NOT NULL,"
        "material_json TEXT NOT NULL, extraction_json TEXT NOT NULL,"
        "recorded_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        "PRIMARY KEY (case_id, material_id));"
    )
    connection.execute(
        "INSERT INTO case_extraction_ledger VALUES (?,?,?,?,?,?)",
        (
            runner.CASE_ID,
            "mat_bridge",
            json.dumps({"id": "mat_bridge"}),
            json.dumps(_BRIDGE_LEDGER_EXTRACTION),
            "2026-09-05T00:00:00+00:00",
            "2026-09-05T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()


def _profiled_options(tmp_path: Path, **overrides: Any) -> runner.RunOptions:
    values: dict[str, Any] = dict(
        material_files=tuple(_material_files(tmp_path)),
        output_dir=tmp_path / "output",
        llm_provider_name="deepseek",
        llm_api_key_env="DEEPSEEK_API_KEY",
        llm_base_url="https://api.deepseek.com/v1",
        llm_model="deepseek-chat",
        bolt_uri="bolt://127.0.0.1:7688",
        http_uri="http://127.0.0.1:7475",
        graphiti_password_env="PRISM_GRAPHITI_PASSWORD",
        provider="synthetic",
    )
    values.update(overrides)
    return runner.RunOptions(**values)


def test_prompt_profile_selection_is_fail_closed() -> None:
    assert runner.resolve_prompt_profile(None) == "baseline"
    assert runner.resolve_prompt_profile("baseline") == "baseline"
    assert runner.resolve_prompt_profile("protocol-v1") == "protocol-v1"
    for bad in ("protocol-v2", "", "Baseline", "PROTOCOL-V1", "protocol v1", 1):
        with pytest.raises(runner.AcceptanceInputError):
            runner.resolve_prompt_profile(bad)


def test_run_id_is_safe_readable_and_not_derived_from_private_input() -> None:
    auto = runner.resolve_run_id(None)
    repeat = runner.resolve_run_id(None)
    # Structure pins the derivation: UTC timestamp plus random token only —
    # never private paths, material content or environment secrets.
    assert re.fullmatch(r"run-\d{8}T\d{6}Z-[0-9a-f]{6}", auto)
    assert auto != repeat
    assert runner.resolve_run_id("live-2026-09-05-a") == "live-2026-09-05-a"
    for bad in ("", "../escape", "run with spaces", "run/01", "E:\\private", "a" * 65):
        with pytest.raises(runner.AcceptanceInputError):
            runner.resolve_run_id(bad)


def test_cli_fails_closed_on_unknown_prompt_profile_or_unsafe_run_id(
    tmp_path: Path,
) -> None:
    argv = [
        "--source-root",
        str(tmp_path),
        "--output-dir",
        str(tmp_path / "out"),
    ]
    assert runner._parse_args(argv).prompt_profile == "baseline"
    assert runner._parse_args(argv).run_id is None
    assert runner.main([*argv, "--prompt-profile", "protocol-v2"]) == 2
    assert runner.main([*argv, "--run-id", "bad run id"]) == 2


def test_default_runtime_factory_passes_prompt_profile_to_create_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_bridge_ledger(tmp_path / "output")
    options = _profiled_options(tmp_path, prompt_profile="protocol-v1", run_id="live-001")
    monkeypatch.delenv("PRISM_HOME", raising=False)
    monkeypatch.setattr(runner, "_optional_dependencies_available", lambda: True)
    monkeypatch.setattr(
        runner,
        "build_real_provider_graphiti_factory",
        lambda opts: (lambda config: object()),
    )
    captured: list[dict[str, Any]] = []
    runtimes = [
        FakeRuntime(FakeAPI(), PrismConfig()),
        FakeRuntime(FakeAPI(), PrismConfig()),
    ]

    async def fake_create_runtime(
        config_path: Path,
        *,
        graphiti_client_factory: Any = None,
        prompt_profile: Any = None,
    ) -> Any:
        captured.append(
            {
                "config_path": str(config_path),
                "prompt_profile": prompt_profile,
            }
        )
        return runtimes.pop(0)

    monkeypatch.setattr(runner, "create_runtime", fake_create_runtime)

    summary = asyncio.run(
        runner.run_acceptance(
            options,
            preflight=lambda: runner.PreflightResult("ready", (), {}),
            quality_gate=lambda home, materials: {
                "verdict": {"mechanism_status": "pass", "semantic_status": "pass"}
            },
        )
    )

    # Both runtime creations (first pass and fresh restart) receive the value.
    assert [call["prompt_profile"] for call in captured] == [
        "protocol-v1",
        "protocol-v1",
    ]
    assert summary["prompt_profile"] == "protocol-v1"
    bridge = json.loads(
        (tmp_path / "output" / "prompt-run-summary.json").read_text(encoding="utf-8")
    )
    assert bridge["profile"] == "protocol-v1"
    assert bridge["run_id"] == "live-001"
    assert bridge["case_id"] == runner.CASE_ID


def test_public_artifacts_label_the_profile_but_never_contain_prompt_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_bridge_ledger(tmp_path / "output")
    options = _profiled_options(tmp_path, prompt_profile="protocol-v1")
    monkeypatch.delenv("PRISM_HOME", raising=False)

    async def runtime_factory(config_path: Path) -> Any:
        return FakeRuntime(FakeAPI(), PrismConfig())

    summary = asyncio.run(
        runner.run_acceptance(
            options,
            runtime_factory=runtime_factory,
            preflight=lambda: runner.PreflightResult("ready", (), {}),
            quality_gate=lambda home, materials: {
                "verdict": {"mechanism_status": "pass", "semantic_status": "pass"}
            },
        )
    )

    assert summary["prompt_profile"] == "protocol-v1"
    output_dir = tmp_path / "output"
    public = ""
    for name in (
        "acceptance-summary.json",
        "acceptance-summary.md",
        "prompt-run-summary.json",
        "run-state.json",
    ):
        public += (output_dir / name).read_text(encoding="utf-8")
    assert "protocol-v1" in public
    # Runtime status and privacy output never carries prompt text.
    assert "SILENT PRE-JSON SELF-CHECK" not in public
    assert "MATERIAL CONTENT" not in public
    assert not re.search(r"(?i)\b(api[_-]?key|password|secret|token)\b", public)


def test_benchmark_reads_the_runners_bridge_file_directly(tmp_path: Path) -> None:
    import prism_prompt_benchmark as benchmark

    _plant_bridge_ledger(tmp_path / "output")
    options = _profiled_options(tmp_path, prompt_profile="protocol-v1", run_id="live-001")

    async def runtime_factory(config_path: Path) -> Any:
        return FakeRuntime(FakeAPI(), PrismConfig())

    asyncio.run(
        runner.run_acceptance(
            options,
            runtime_factory=runtime_factory,
            preflight=lambda: runner.PreflightResult("ready", (), {}),
            quality_gate=lambda home, materials: {
                "verdict": {"mechanism_status": "pass", "semantic_status": "pass"},
                "coverage": {"source_ids": {"rate": 1.0}},
            },
        )
    )

    bridge_path = tmp_path / "output" / "prompt-run-summary.json"
    report = benchmark.build_report([bridge_path])

    assert report["inputs"]["run_summaries"] == 1
    assert report["profiles"]["protocol-v1"]["cases"][runner.CASE_ID]["runs"] == 1
    assert report["verdict"]["stability_status"] == "insufficient_runs"


def test_no_bridge_is_written_when_the_run_fails(tmp_path: Path) -> None:
    _plant_bridge_ledger(tmp_path / "output")
    options = _profiled_options(tmp_path)

    async def runtime_factory(config_path: Path) -> Any:
        return FakeRuntime(FakeAPI(), PrismConfig())

    summary = asyncio.run(
        runner.run_acceptance(
            options,
            runtime_factory=runtime_factory,
            preflight=lambda: runner.PreflightResult("ready", (), {}),
            quality_gate=lambda home, materials: {
                "verdict": {"mechanism_status": "fail", "semantic_status": "fail"}
            },
        )
    )

    assert summary["overall_status"] == "fail"
    assert not (tmp_path / "output" / "prompt-run-summary.json").exists()
