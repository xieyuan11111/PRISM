"""TDD tests for the split-v1 mode of the prompt-profile experiment runner."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import run_prompt_profile_experiment as experiment  # noqa: E402
from prism.llm import TransportResponse  # noqa: E402

CASE_ID = experiment.CASE_ID
NODE_QUOTE = "The agency published a revised policy."
FACT_QUOTE = "The agency implemented a revised policy."
CLAIM_QUOTE = "Analysts said the policy may expand next year."
RELATION_QUOTE = "The implementation supersedes the proposal."
MATERIAL_ID_RE = re.compile(r"MATERIAL ID: ([A-Za-z0-9_.-]+)")

LEAK_MARKERS = (
    "SECRET-MATERIAL-qW7",
    "SECRET-SUMMARY-vB3",
    "SECRET-BAD-CLAIM",
    "E:\\private\\corpus\\material.md",
    "offline-test-key",
)

MATERIAL_BODY = (
    "# Policy update SECRET-MATERIAL-qW7\n\n"
    "The agency published a revised policy. Source file at "
    "E:\\private\\corpus\\material.md.\n\n"
    "The agency implemented a revised policy.\n\n"
    "Analysts said the policy may expand next year.\n\n"
    "The implementation supersedes the proposal.\n"
)


def evidence(material_id: str, quote: str, paragraph: int) -> list[dict]:
    return [
        {
            "source_id": material_id,
            "quote": quote,
            "paragraph": paragraph,
            "page": None,
        }
    ]


def stage_a(material_id: str) -> dict:
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
                "evidence": evidence(material_id, NODE_QUOTE, 2),
            }
        ],
        "temporal_facts": [
            {
                "fact_id": "policy-2026-implementation",
                "subject": "Revised policy",
                "predicate": "implementation_status",
                "object": "implemented",
                "assertion_type": "fact",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "invalid_at": None,
                "observed_at": "2026-02-15T00:00:00+00:00",
                "source_ids": [material_id],
                "confidence": 0.95,
                "provenance_type": "source_explicit",
                "evidence": evidence(material_id, FACT_QUOTE, 3),
            }
        ],
        "warnings": [],
    }


def stage_b(material_id: str) -> dict:
    return {
        "claims": [
            {
                "claim_id": "claim-forecast",
                "actor": "Analysts",
                "proposition": "The policy may expand next year.",
                "stance": "uncertain",
                "claim_type": "prediction",
                "stated_at": "2026-02-15T00:00:00+00:00",
                "observed_at": "2026-02-15T00:00:00+00:00",
                "based_on": [material_id],
                "revised_by": None,
                "provenance_type": "reported",
                "confidence": 0.7,
                "evidence": evidence(material_id, CLAIM_QUOTE, 4),
            }
        ],
        "conflicts": [],
        "relations": [
            {
                "relation_id": "relation-supersedes",
                "relation_type": "supersedes",
                "source_ref": "policy-2026-proposal",
                "target_ref": "policy-2026-implementation",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "invalid_at": None,
                "observed_at": "2026-02-15T00:00:00+00:00",
                "source_ids": [material_id],
                "evidence": evidence(material_id, RELATION_QUOTE, 5),
                "confidence": 0.9,
                "provenance_type": "source_explicit",
            }
        ],
        "warnings": [],
    }


class SplitTransportFactory:
    instances: list["SplitTransportFactory.Transport"] = []

    class Transport:
        def __init__(self, *, stream: bool = False, json_mode: bool = False):
            self.stream = stream
            self.json_mode = json_mode
            self.calls: list[dict] = []
            self.invalid_stage: str | None = None

        async def complete(self, *, provider, api_key, payload, timeout):
            self.calls.append(
                {
                    "provider": provider,
                    "api_key": api_key,
                    "payload": payload,
                    "timeout": timeout,
                }
            )
            prompt = payload["prompt"]
            match = MATERIAL_ID_RE.search(prompt)
            material_id = match.group(1) if match else "mat_unknown"
            if self.invalid_stage == "stage_a" and "SPLIT-V1 STAGE A" in prompt:
                return TransportResponse(
                    text='{"material_role":"policy_source","claims":[]'
                )
            if self.invalid_stage == "stage_b" and "SPLIT-V1 STAGE B" in prompt:
                return TransportResponse(
                    text='{"claims":[{"claim_id":"SECRET-BAD-CLAIM"}'
                )
            if "SPLIT-V1 STAGE A" in prompt:
                return TransportResponse(text=json.dumps(stage_a(material_id)))
            if "SPLIT-V1 STAGE B" in prompt:
                return TransportResponse(text=json.dumps(stage_b(material_id)))
            raise AssertionError("split-v1 experiment unexpectedly used another prompt")

        async def test_connection(self, *, provider, api_key, timeout):
            return True

    def __call__(self, **kwargs):
        transport = self.Transport(**kwargs)
        self.instances.append(transport)
        return transport


def base_args(source_root: Path, output_dir: Path) -> list[str]:
    return [
        "--source-root",
        str(source_root),
        "--output-dir",
        str(output_dir),
        "--llm-api-key-env",
        "PRISM_TEST_EXP_KEY",
        "--llm-base-url",
        "https://example.test/api",
        "--llm-model",
        "provider/model-v1",
        "--split-v1",
    ]


def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    corpus = root / "corpus"
    corpus.mkdir(parents=True)
    for name in ("alpha-mat-01.md", "beta-mat-02.md"):
        (corpus / name).write_text(MATERIAL_BODY, encoding="utf-8")
    return root


def test_plan_mode_records_split_v1_without_runtime(tmp_path, monkeypatch):
    output_dir = tmp_path / "out"
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "present")

    def fail_runtime(*args, **kwargs):
        raise AssertionError("plan mode must not create a runtime")

    monkeypatch.setattr(experiment, "create_runtime", fail_runtime)

    assert experiment.main(base_args(source_root(tmp_path), output_dir)) == 0

    plan = json.loads((output_dir / "run-plan.json").read_text(encoding="utf-8"))
    assert plan["split_v1"] is True
    assert plan["graph_backend"] == "offline"


def test_split_v1_uses_two_official_sdk_completions_per_material(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "out"
    factory = SplitTransportFactory()
    monkeypatch.setattr(experiment, "OpenAISDKTransport", factory)
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "offline-test-key")

    assert experiment.main(base_args(source_root(tmp_path), output_dir) + ["--execute"]) == 0

    transport = factory.instances[-1]
    assert len(transport.calls) == 4
    assert [call["payload"]["model"] for call in transport.calls] == [
        "provider/model-v1"
    ] * 4
    assert [
        "stage_a" if "SPLIT-V1 STAGE A" in call["payload"]["prompt"] else "stage_b"
        for call in transport.calls
    ] == ["stage_a", "stage_b"] * 2

    bridge = json.loads(
        (output_dir / "prompt-run-summary.json").read_text(encoding="utf-8")
    )
    assert bridge["candidates"]["node"]["ids"] == ["policy-2026-proposal"]
    assert bridge["candidates"]["temporal_fact"]["ids"] == [
        "policy-2026-implementation"
    ]
    assert bridge["candidates"]["claim"]["ids"] == ["claim-forecast"]
    assert bridge["candidates"]["relation"]["ids"] == ["relation-supersedes"]
    assert bridge["gap_types"] == {}

    rendered = json.dumps(bridge)
    for marker in LEAK_MARKERS:
        assert marker not in rendered


def test_stage_a_failure_is_recorded_without_calling_stage_b(tmp_path, monkeypatch):
    output_dir = tmp_path / "out"
    factory = SplitTransportFactory()
    monkeypatch.setattr(experiment, "OpenAISDKTransport", factory)
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "offline-test-key")

    def invalid_transport(**kwargs):
        transport = factory.Transport(**kwargs)
        transport.invalid_stage = "stage_a"
        factory.instances.append(transport)
        return transport

    monkeypatch.setattr(experiment, "OpenAISDKTransport", invalid_transport)

    assert experiment.main(base_args(source_root(tmp_path), output_dir) + ["--execute"]) == 0
    transport = factory.instances[-1]
    assert len(transport.calls) == 2
    assert all("SPLIT-V1 STAGE A" in call["payload"]["prompt"] for call in transport.calls)

    bridge = json.loads(
        (output_dir / "prompt-run-summary.json").read_text(encoding="utf-8")
    )
    assert bridge["gap_types"] == {"stage_a_failure": 2}
    assert bridge["candidates"]["node"]["ids"] == []
    rendered = json.dumps(bridge)
    for marker in LEAK_MARKERS:
        assert marker not in rendered


def test_stage_b_failure_preserves_stage_a_and_records_sanitized_gap(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "out"
    factory = SplitTransportFactory()
    monkeypatch.setattr(experiment, "OpenAISDKTransport", factory)
    monkeypatch.setenv("PRISM_TEST_EXP_KEY", "offline-test-key")

    def invalid_transport(**kwargs):
        transport = factory.Transport(**kwargs)
        transport.invalid_stage = "stage_b"
        factory.instances.append(transport)
        return transport

    monkeypatch.setattr(experiment, "OpenAISDKTransport", invalid_transport)

    assert experiment.main(base_args(source_root(tmp_path), output_dir) + ["--execute"]) == 0
    transport = factory.instances[-1]
    assert len(transport.calls) == 4

    bridge = json.loads(
        (output_dir / "prompt-run-summary.json").read_text(encoding="utf-8")
    )
    assert bridge["candidates"]["node"]["ids"] == ["policy-2026-proposal"]
    assert bridge["candidates"]["temporal_fact"]["ids"] == [
        "policy-2026-implementation"
    ]
    assert bridge["candidates"]["claim"]["ids"] == []
    assert bridge["gap_types"] == {"stage_b_failure": 2}
    rendered = json.dumps(bridge)
    for marker in LEAK_MARKERS:
        assert marker not in rendered
