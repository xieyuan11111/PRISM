"""TDD tests for tools/run_prompt_profile_experiment.py.

The experiment tool must reuse PRISM's one LLM path — the official-SDK
:class:`~prism.llm.OpenAISDKTransport` plus :class:`~prism.llm.LLMRouter`
through the normal composition root — while evaluating ONLY the extraction
service over an offline runtime (OfflineGraphBackend, Graphiti never
started).  Without ``--execute`` it is a pure validation/dry-run that never
creates a runtime, never imports the openai SDK and never touches the
network.  Its only public artifact is a strictly sanitized
``prompt-run-summary.json``.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import prism_prompt_benchmark as benchmark  # noqa: E402
import run_prompt_profile_experiment as experiment  # noqa: E402

from prism.llm import TransportResponse  # noqa: E402

CASE_ID = experiment.CASE_ID


def test_default_experiment_case_window_includes_the_beijing_probe_material():
    """The shipped probe's policy event is on 2025-12-24, not 2026-01-01."""
    assert experiment.CASE_START_AT <= datetime.fromisoformat(
        "2025-12-24T00:00:00+00:00"
    )

LEAK_MARKERS = (
    "SECRET-MATERIAL-qW7",
    "SECRET-SUMMARY-vB3",
    "E:\\private\\corpus\\material.md",
    "/home/xieyu/corpus/material.md",
    "PRISM_TEST_EXP_KEY",
)

# The strict evolution-extraction completion contract (see
# tests/test_evolution_extraction_v0.py): material_role + case + evidence-
# bound node; ids/types only except the evidence quote, which must exist
# verbatim in the material body so the locator can bind it.
NODE_QUOTE = "The agency published a revised policy."
_MATERIAL_ID_IN_PROMPT = re.compile(r"MATERIAL ID: ([A-Za-z0-9_.-]+)")


def extraction_payload(material_id: str) -> dict:
    # The completion's case must anchor exactly to the tool's declared
    # target case (id, type, name, start_at and status) — PRISM refuses
    # drifted cases, so a well-behaved fake echoes the declared identity.
    return {
        "material_role": "policy_source",
        "case": {
            "case_id": CASE_ID,
            "case_type": experiment.CASE_TYPE,
            "canonical_name": experiment.CASE_NAME,
            "start_at": experiment.CASE_START_AT.isoformat(),
            "status": "active",
            "node_ids": ["policy-2026-proposal"],
        },
        "nodes": [
            {
                "id": "policy-2026-proposal",
                "case_id": CASE_ID,
                "node_type": "proposal",
                "assertion_type": "fact",
                "happened_at": "2026-02-15T00:00:00+00:00",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "observed_at": "2026-02-15T00:00:00+00:00",
                "summary": "SECRET-SUMMARY-vB3 the proposal",
                "source_ids": [material_id],
                "claim_ids": [],
                "provenance_type": "source_explicit",
                "evidence": [
                    {
                        "source_id": material_id,
                        "quote": NODE_QUOTE,
                        "paragraph": 2,
                        "page": None,
                    }
                ],
            }
        ],
        "temporal_facts": [],
        "claims": [],
        "conflicts": [],
        "relations": [],
        "warnings": [],
    }

MATERIAL_BODY = (
    "# Policy update SECRET-MATERIAL-qW7\n\n"
    "The agency published a revised policy. Source file at "
    "E:\\private\\corpus\\material.md.\n"
)


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    corpus = root / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "alpha-mat-01.md").write_text(MATERIAL_BODY, encoding="utf-8")
    (corpus / "beta-mat-02.md").write_text(MATERIAL_BODY, encoding="utf-8")
    return root


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "out"


def base_args(source_root: Path, output_dir: Path) -> list[str]:
    return [
        "--source-root",
        str(source_root),
        "--output-dir",
        str(output_dir),
        "--llm-provider",
        "primary",
        "--llm-api-key-env",
        "PRISM_TEST_EXP_KEY",
        "--llm-base-url",
        "https://ark.cn.volces.com/api/coding/v3",
        "--llm-model",
        "provider/model-v1",
    ]


class RecordingTransportFactory:
    """Stand-in for OpenAISDKTransport that records how it was built."""

    instances: list["RecordingTransportFactory.Transport"] = []

    class Transport:
        def __init__(self, *, stream: bool = False, json_mode: bool = False) -> None:
            self.stream = stream
            self.json_mode = json_mode
            self.calls: list[dict] = []

        async def complete(self, *, provider, api_key, payload, timeout):
            self.calls.append(
                {
                    "provider": provider,
                    "api_key": api_key,
                    "payload": payload,
                    "timeout": timeout,
                }
            )
            # A well-behaved fake model: read the material id PRISM put in
            # the prompt and answer with a contract-valid extraction.
            match = _MATERIAL_ID_IN_PROMPT.search(payload["prompt"])
            material_id = match.group(1) if match else "mat_unknown"
            return TransportResponse(text=json.dumps(extraction_payload(material_id)))

        async def test_connection(self, *, provider, api_key, timeout):
            return True

    def __call__(self, **kwargs):
        transport = self.Transport(**kwargs)
        self.instances.append(transport)
        return transport


@pytest.fixture
def fake_sdk_transport(monkeypatch) -> RecordingTransportFactory:
    factory = RecordingTransportFactory()
    monkeypatch.setattr(experiment, "OpenAISDKTransport", factory)
    return factory


# ------------------------------------------------------------------ plan


def test_plan_mode_validates_without_creating_a_runtime_or_importing_sdk(
    source_root, output_dir, monkeypatch, fake_sdk_transport
):
    for name in [key for key in sys.modules if key.startswith("openai")]:
        monkeypatch.delitem(sys.modules, name)

    def fail_factory(*args, **kwargs):
        raise AssertionError("plan mode must not create a runtime")

    monkeypatch.setattr(experiment, "create_runtime", fail_factory)
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "present")

    code = experiment.main(base_args(source_root, output_dir))

    assert code == 0
    assert fake_sdk_transport.instances == []
    assert not any(key.startswith("openai") for key in sys.modules)
    plan = json.loads((output_dir / "run-plan.json").read_text(encoding="utf-8"))
    assert plan["execute"] is False
    assert plan["sdk_stream"] is False
    assert plan["sdk_json_mode"] is False
    assert plan["profile"] == "baseline"
    assert plan["case_id"] == CASE_ID
    assert plan["materials"] == 2
    assert plan["graph_backend"] == "offline"
    assert plan["ready"] is True
    rendered = json.dumps(plan)
    for marker in LEAK_MARKERS:
        assert marker not in rendered


def test_plan_mode_reports_missing_api_key_env_as_not_ready(
    source_root, output_dir, monkeypatch
):
    monkeypatch.delenv("PRISM_TEST_EXP_KEY", raising=False)
    code = experiment.main(base_args(source_root, output_dir))
    assert code == 0
    plan = json.loads((output_dir / "run-plan.json").read_text(encoding="utf-8"))
    assert plan["ready"] is False
    assert any("PRISM_TEST_EXP_KEY" in reason for reason in plan["reasons"])


def test_unknown_prompt_profile_fails_closed(
    source_root, output_dir, monkeypatch
):
    def fail_factory(*args, **kwargs):
        raise AssertionError("invalid input must not create a runtime")

    monkeypatch.setattr(experiment, "create_runtime", fail_factory)
    code = experiment.main(
        base_args(source_root, output_dir) + ["--prompt-profile", "does-not-exist"]
    )
    assert code == 2


def test_execute_without_api_key_env_is_blocked_before_any_runtime(
    source_root, output_dir, monkeypatch
):
    monkeypatch.delenv("PRISM_TEST_EXP_KEY", raising=False)

    def fail_factory(*args, **kwargs):
        raise AssertionError("blocked run must not create a runtime")

    monkeypatch.setattr(experiment, "create_runtime", fail_factory)
    code = experiment.main(base_args(source_root, output_dir) + ["--execute"])
    assert code == 2


# --------------------------------------------------------------- execute


def test_execute_runs_extraction_offline_and_writes_sanitized_summary(
    source_root, output_dir, monkeypatch, fake_sdk_transport
):
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "offline-test-key")
    monkeypatch.setenv("PRISM_HOME", str(output_dir / "unrelated-home"))

    code = experiment.main(
        base_args(source_root, output_dir)
        + ["--execute", "--prompt-profile", "protocol-v1", "--run-id", "run-01"]
    )

    assert code == 0
    (transport,) = fake_sdk_transport.instances
    # The extraction service drove the router, which drove the (fake) SDK
    # transport: one completion per material through the unified path.
    assert len(transport.calls) == 2
    assert all("SPLIT-V1" not in call["payload"]["prompt"] for call in transport.calls)
    assert transport.calls[0]["payload"]["model"] == "provider/model-v1"
    assert "Policy update" in transport.calls[0]["payload"]["prompt"]
    assert transport.calls[0]["provider"].base_url == (
        "https://ark.cn.volces.com/api/coding/v3"
    )

    bridge_path = output_dir / "prompt-run-summary.json"
    bridge = json.loads(bridge_path.read_text(encoding="utf-8"))
    assert bridge["profile"] == "protocol-v1"
    assert bridge["run_id"] == "run-01"
    assert bridge["case_id"] == CASE_ID
    assert bridge["candidates"]["node"]["ids"] == ["policy-2026-proposal"]
    assert bridge["candidates"]["node"]["types"] == {"proposal": 2}
    assert bridge["gap_types"] == {}
    assert "mechanism_status" in bridge["verdict"]
    assert "semantic_status" in bridge["verdict"]

    rendered = bridge_path.read_text(encoding="utf-8")
    for marker in LEAK_MARKERS:
        assert marker not in rendered
    for forbidden_key in ("quote", "summary", "payload", "prompt", "corpus_path"):
        assert f'"{forbidden_key}"' not in rendered

    # The bridge is directly consumable by the offline benchmark tool.
    report = benchmark.build_report([bridge_path])
    assert report["verdict"]["stability_status"] in {
        "stable",
        "unstable",
        "insufficient_runs",
    }


def test_execute_keeps_the_run_offline_with_the_local_graph_backend(
    source_root, output_dir, monkeypatch, fake_sdk_transport
):
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "offline-test-key")

    assert (
        experiment.main(base_args(source_root, output_dir) + ["--execute"]) == 0
    )
    # The run home lives under the output dir; the tool's own config keeps
    # Graphiti disabled (the runtime composes OfflineGraphBackend and the
    # tool fails closed on anything else) and restores PRISM_HOME.
    assert (output_dir / "prism-home" / "data" / "index.db").is_file()
    config = json.loads(
        (output_dir / "prism-home" / "config.json").read_text(encoding="utf-8")
    )
    assert config["graphiti"]["enabled"] is False


def test_sdk_stream_flag_reaches_the_sdk_transport(
    source_root, output_dir, monkeypatch, fake_sdk_transport
):
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "offline-test-key")

    assert experiment.main(base_args(source_root, output_dir) + ["--execute"]) == 0
    assert fake_sdk_transport.instances[-1].stream is False

    fresh_output = output_dir.parent / "out-stream"
    assert (
        experiment.main(
            base_args(source_root, fresh_output) + ["--execute", "--sdk-stream"]
        )
        == 0
    )
    assert fake_sdk_transport.instances[-1].stream is True


def test_sdk_json_mode_flag_reaches_the_sdk_transport(
    source_root, output_dir, monkeypatch, fake_sdk_transport
):
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "offline-test-key")

    assert (
        experiment.main(
            base_args(source_root, output_dir)
            + ["--execute", "--sdk-stream", "--sdk-json-mode"]
        )
        == 0
    )
    transport = fake_sdk_transport.instances[-1]
    assert transport.stream is True
    assert transport.json_mode is True


def test_execute_never_reveals_the_api_key_in_its_artifacts(
    source_root, output_dir, monkeypatch, fake_sdk_transport
):
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "sk-live-never-leak")
    assert experiment.main(base_args(source_root, output_dir) + ["--execute"]) == 0

    for artifact in output_dir.glob("*.json"):
        assert "sk-live-never-leak" not in artifact.read_text(encoding="utf-8")


def test_failed_materials_are_projected_as_sanitized_pipeline_gaps():
    extractions = ({"nodes": [], "evidence_gaps": []},)
    bridge = experiment.acceptance.build_prompt_run_summary(
        profile="baseline",
        run_id="run-01",
        case_id=CASE_ID,
        extractions=extractions
        + ({"evidence_gaps": [{"gap_type": "pipeline_failure"}]},),
        quality={
            "verdict": {"mechanism_status": "fail", "semantic_status": "fail"}
        },
    )

    assert bridge["gap_types"] == {"pipeline_failure": 1}
