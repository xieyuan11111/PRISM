"""Opt-in live integration tests for the Phase B Graphiti/Neo4j spike.

These tests run ONLY when both ``PRISM_GRAPHITI_URI`` and
``PRISM_GRAPHITI_PASSWORD`` are actually set in the environment.  Default CI
never sets them, so this module always skips in a normal
``python -m pytest -q`` run and never enters default CI.

They also require the optional ``[graphiti]`` dependencies and a live,
PRISM-OWNED Neo4j/Graphiti instance reachable at ``PRISM_GRAPHITI_URI``
(see deploy/graphiti-spike and docs/graphiti-spike-plan.md).

Phase A honesty note: the real Graphiti integration has NOT been verified
yet.  A first live run is expected to surface API mismatches (the plan doc
keeps a PHASE B VERIFY list); treat failures here as spike findings to
reconcile, not as evidence the adapter is broken by itself.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from prism.config import GraphitiConfig, PrismConfig
from prism.domain import EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.runtime import create_runtime

_REQUIRED_ENV = ("PRISM_GRAPHITI_URI", "PRISM_GRAPHITI_PASSWORD")
_LIVE = all(os.environ.get(name) for name in _REQUIRED_ENV)

if _LIVE:
    # Only touch the optional packages when the operator actually asked for a
    # live run; the default (skipped) path never imports them.
    pytest.importorskip("graphiti_core")
    pytest.importorskip("neo4j")

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "live Graphiti spike requires PRISM_GRAPHITI_URI and "
        "PRISM_GRAPHITI_PASSWORD to be set"
    ),
)

T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)


def run(coro):
    return asyncio.run(coro)


def live_config(tmp_path) -> tuple[PrismConfig, object]:
    """Build a config pointing at the PRISM-owned live instance.

    The group id is explicit and test-scoped so repeated live runs on the
    same instance do not collide with spike data.
    """
    config = PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri=os.environ["PRISM_GRAPHITI_URI"],
            database=os.environ.get("PRISM_GRAPHITI_DATABASE", ""),
            group_id=os.environ.get("PRISM_GRAPHITI_GROUP", "prism-live-integration"),
            password_env="PRISM_GRAPHITI_PASSWORD",
        )
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)
    return config, config_path


def fixtures():
    case = EvolutionCase(
        "live-spike-case",
        "policy",
        "Live spike housing policy",
        T0,
        "active",
        ["live-node-publish"],
    )
    node = EvolutionNode(
        "live-node-publish",
        case.case_id,
        "publication",
        T0 + timedelta(hours=1),
        "The live spike policy was published.",
        ["live-material-a"],
    )
    fact = TemporalFact(
        "Agency",
        "requires",
        "Disclosure",
        T0 + timedelta(hours=2),
        None,
        T0 + timedelta(hours=2),
        ["live-material-a"],
        0.9,
        "document",
    )
    material = Material(
        id="live-material-a",
        title="Live spike notice",
        source="example.gov",
        published_at=T0 - timedelta(hours=1),
        fetched_at=T0,
        type="policy",
        content="Public live spike body",
        original_format="md",
    )
    return case, node, fact, material


def test_live_write_read_restart_and_idempotent_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    _, config_path = live_config(tmp_path)
    case, node, fact, material = fixtures()

    async def exercise():
        first = await create_runtime(config_path)
        try:
            result = await first.graph.add_case(
                case, nodes=[node], facts=[fact], materials=[material]
            )
            assert result.added_keys, "live spike: nothing was written"
            # In-process duplicate write is a no-op.
            duplicate = await first.graph.add_case(
                case, nodes=[node], facts=[fact], materials=[material]
            )
            assert duplicate.added_keys == ()
            before = await first.graph.timeline(case.case_id, T0 + timedelta(days=1))
            assert any(entry.kind == "temporal_fact" for entry in before.entries)
        finally:
            await first.close()

        # Restart: a fresh runtime and driver must reconstruct the same state
        # from the live graph (PRISM schema bodies or registry mapping).
        second = await create_runtime(config_path)
        try:
            after = await second.graph.timeline(case.case_id, T0 + timedelta(days=1))
            assert [entry.episode_key for entry in after.entries] == [
                entry.episode_key for entry in before.entries
            ]
        finally:
            await second.close()

    run(exercise())


def test_live_historical_cutoffs_exclude_invalidated_and_unobserved(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    _, config_path = live_config(tmp_path)
    case = EvolutionCase(
        "live-spike-cutoffs", "policy", "Cutoff policy", T0, "active", ()
    )
    fact_v1 = TemporalFact(
        "Agency",
        "sets",
        "Rate=3%",
        T0,
        T0 + timedelta(days=3),
        T0,
        ("live-cutoff-material-a",),
        0.9,
        "document",
    )
    fact_v2 = TemporalFact(
        "Agency",
        "sets",
        "Rate=3%",
        T0 + timedelta(days=3),
        None,
        T0 + timedelta(days=2),
        ("live-cutoff-material-b",),
        0.9,
        "document",
    )
    material_a = Material(
        id="live-cutoff-material-a",
        title="Cutoff notice A",
        source="example.gov",
        published_at=T0 - timedelta(hours=1),
        fetched_at=T0,
        type="policy",
        content="body",
        original_format="md",
    )
    material_b = Material(
        id="live-cutoff-material-b",
        title="Cutoff notice B",
        source="example.gov",
        published_at=T0 + timedelta(days=2),
        fetched_at=T0 + timedelta(days=2),
        type="policy",
        content="body",
        original_format="md",
    )

    async def exercise():
        runtime = await create_runtime(config_path)
        try:
            await runtime.graph.add_case(
                case,
                facts=[fact_v1, fact_v2],
                materials=[material_a, material_b],
            )
            early = await runtime.graph.timeline(case.case_id, T0 + timedelta(days=1))
            late = await runtime.graph.timeline(case.case_id, T0 + timedelta(days=10))
        finally:
            await runtime.close()

        early_keys = {
            entry.episode_key
            for entry in early.entries
            if entry.kind == "temporal_fact"
        }
        late_keys = {
            entry.episode_key
            for entry in late.entries
            if entry.kind == "temporal_fact"
        }
        assert early_keys and early_keys.isdisjoint(late_keys)

    run(exercise())
