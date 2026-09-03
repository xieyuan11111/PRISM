"""Opt-in live integration tests for the Phase B Graphiti/Neo4j spike.

These tests run ONLY when both ``PRISM_GRAPHITI_URI`` and
``PRISM_GRAPHITI_PASSWORD`` are actually set in the environment.  Default CI
never sets them, so this module always skips in a normal
``python -m pytest -q`` run and never enters default CI.

They also require the optional ``[graphiti]`` dependencies and a live,
PRISM-OWNED Neo4j/Graphiti instance reachable at ``PRISM_GRAPHITI_URI``
(see deploy/graphiti-spike and docs/graphiti-spike-plan.md).

Community live semantics: the PRISM-owned container runs Neo4j Community,
which serves ONE built-in database.  graphiti-core 0.29.3 realises a Neo4j
group as a database (``add_episode`` clones the driver to
``database=group_id`` whenever an explicit group_id differs), so live configs
here use group_id == database == "neo4j" and GraphitiConfig rejects any
mismatch.  Two-group isolation is therefore NOT a live Community acceptance
item: isolation is the PRISM-dedicated instance itself plus PRISM schema
marker gating on search mapping (both exercised offline as pure adapter
contracts in test_graphiti_backend.py).

Phase B live status (2026-09-03, standalone local machine spike): the three
tests in this module passed against the PRISM-owned Neo4j Community 5.26
instance (HTTP 127.0.0.1:7475 / Bolt 127.0.0.1:7688, local-only) with
graphiti-core 0.29.3 and the neo4j Python driver 6.3.0.  They run through the
deterministic provider clients in ``graphiti_live_deterministic.py`` so no
real LLM/embedding/rerank API is ever called (``OPENAI_API_KEY`` is removed
from the environment below); a real-model extraction and a real-case
end-to-end rerun remain unverified.  Every case/material/fact id in this
module carries a random per-run suffix, so reruns never collide with data
left by earlier runs in the persistent spike database.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from prism.config import GraphitiConfig, PrismConfig
from prism.domain import (
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
    TemporalRelation,
)
from prism.runtime import create_runtime

_REQUIRED_ENV = ("PRISM_GRAPHITI_URI", "PRISM_GRAPHITI_PASSWORD")
_LIVE = all(os.environ.get(name) for name in _REQUIRED_ENV)

if _LIVE:
    # Only touch the optional packages when the operator actually asked for a
    # live run; the default (skipped) path never imports them.
    pytest.importorskip("graphiti_core")
    pytest.importorskip("neo4j")
    from graphiti_live_deterministic import DeterministicGraphitiClientFactory

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


def live_run_suffix() -> str:
    """Return a secret-free run marker that remains recognizable in Neo4j."""
    return uuid4().hex[:12]


def live_config(tmp_path) -> tuple[PrismConfig, object]:
    """Build a config pointing at the PRISM-owned live instance.

    Community shape: group_id == database == "neo4j" (the single built-in
    database of the PRISM-owned container).  graphiti-core 0.29.3 realises a
    Neo4j group as a database, so GraphitiConfig rejects any live config
    whose group differs from its database; an operator may override both
    names together (PRISM_GRAPHITI_DATABASE and PRISM_GRAPHITI_GROUP) only
    when the server really serves that database.  Reruns are write-no-ops by
    PRISM key because create_runtime injects the persistent SQLite registry:
    the second runtime short-circuits every duplicate write before the
    client.  (Before the registry existed, a rerun added duplicate graph
    nodes under fresh Graphiti-assigned uuids; that was harmless for the
    assertions here because every episode body carries the deterministic
    PRISM episode_key and search dedups by it.)
    """
    database = os.environ.get("PRISM_GRAPHITI_DATABASE", "neo4j")
    group_id = os.environ.get("PRISM_GRAPHITI_GROUP", database)
    config = PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri=os.environ["PRISM_GRAPHITI_URI"],
            database=database,
            group_id=group_id,
            password_env="PRISM_GRAPHITI_PASSWORD",
        )
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)
    return config, config_path


def fixtures():
    suffix = live_run_suffix()
    case_id = f"live-spike-case-{suffix}"
    node_id = f"live-node-publish-{suffix}"
    material_id = f"live-material-a-{suffix}"
    case = EvolutionCase(
        case_id,
        "policy",
        "Live spike housing policy",
        T0,
        "active",
        [node_id],
    )
    node = EvolutionNode(
        node_id,
        case.case_id,
        "publication",
        T0 + timedelta(hours=1),
        "The live spike policy was published.",
        [material_id],
    )
    fact = TemporalFact(
        "Agency",
        "requires",
        "Disclosure",
        T0 + timedelta(hours=2),
        None,
        T0 + timedelta(hours=2),
        [material_id],
        0.9,
        "document",
    )
    material = Material(
        id=material_id,
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
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GRAPHITI_TELEMETRY_ENABLED", "false")
    _, config_path = live_config(tmp_path)
    case, node, fact, material = fixtures()
    factory = DeterministicGraphitiClientFactory()

    async def exercise():
        first = await create_runtime(
            config_path, graphiti_client_factory=factory
        )
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
        second = await create_runtime(
            config_path, graphiti_client_factory=factory
        )
        try:
            after = await second.graph.timeline(case.case_id, T0 + timedelta(days=1))
            assert [entry.episode_key for entry in after.entries] == [
                entry.episode_key for entry in before.entries
            ]
            assert factory.calls == [first.config.graphiti, second.config.graphiti]
            # The restarted runtime only reads, so its LLM is correctly idle;
            # both clients still use the deterministic embedder for search.
            assert factory.llm_clients[0].response_models
            assert all(embedder.calls for embedder in factory.embedders)
        finally:
            await second.close()

    run(exercise())


def test_live_historical_cutoffs_exclude_invalidated_and_unobserved(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GRAPHITI_TELEMETRY_ENABLED", "false")
    _, config_path = live_config(tmp_path)
    factory = DeterministicGraphitiClientFactory()
    suffix = live_run_suffix()
    case_id = f"live-spike-cutoffs-{suffix}"
    material_a_id = f"live-cutoff-material-a-{suffix}"
    material_b_id = f"live-cutoff-material-b-{suffix}"
    old_fact_id = f"live-cutoff-fact-old-{suffix}"
    new_fact_id = f"live-cutoff-fact-new-{suffix}"
    case = EvolutionCase(
        case_id, "policy", "Cutoff policy", T0, "active", ()
    )
    fact_v1 = TemporalFact(
        "Agency",
        "sets",
        "Rate=3%",
        T0,
        T0 + timedelta(days=3),
        T0,
        (material_a_id,),
        0.9,
        "document",
        fact_id=old_fact_id,
    )
    fact_v2 = TemporalFact(
        "Agency",
        "sets",
        "Rate=3%",
        T0 + timedelta(days=3),
        None,
        T0 + timedelta(days=2),
        (material_b_id,),
        0.9,
        "document",
        fact_id=new_fact_id,
    )
    material_a = Material(
        id=material_a_id,
        title="Cutoff notice A",
        source="example.gov",
        published_at=T0 - timedelta(hours=1),
        fetched_at=T0,
        type="policy",
        content="body",
        original_format="md",
    )
    material_b = Material(
        id=material_b_id,
        title="Cutoff notice B",
        source="example.gov",
        published_at=T0 + timedelta(days=2),
        fetched_at=T0 + timedelta(days=2),
        type="policy",
        content="body",
        original_format="md",
    )

    async def exercise():
        runtime = await create_runtime(
            config_path, graphiti_client_factory=factory
        )
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
        assert {
            json.loads(entry.payload).get("fact_id")
            for entry in early.entries
            if entry.kind == "temporal_fact"
        } == {old_fact_id}
        assert {
            json.loads(entry.payload).get("fact_id")
            for entry in late.entries
            if entry.kind == "temporal_fact"
        } == {new_fact_id}
        assert {
            json.loads(entry.payload).get("fact_id")
            for entry in late.invalidated_entries
            if entry.kind == "temporal_fact"
        } == {old_fact_id}
        assert factory.calls == [runtime.config.graphiti]
        assert factory.llm_clients[0].response_models
        assert factory.embedders[0].calls

    run(exercise())


def test_live_m1_relations_survive_cutoffs_and_registry_restart(
    tmp_path, monkeypatch
):
    """Real Graphiti preserves M1 facts, relations and portable evidence."""
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GRAPHITI_TELEMETRY_ENABLED", "false")
    _, config_path = live_config(tmp_path)
    factory = DeterministicGraphitiClientFactory()
    suffix = live_run_suffix()
    case_id = f"live-m1-case-{suffix}"
    old_material_id = f"live-m1-material-old-{suffix}"
    new_material_id = f"live-m1-material-new-{suffix}"
    old_fact_id = f"live-m1-fact-old-{suffix}"
    new_fact_id = f"live-m1-fact-new-{suffix}"
    supersedes_id = f"live-m1-supersedes-{suffix}"
    contradicts_id = f"live-m1-contradicts-{suffix}"
    revision_at = T0 + timedelta(days=3)
    early_cutoff = revision_at - timedelta(seconds=1)
    late_cutoff = revision_at + timedelta(days=1)
    old_evidence = EvidenceLocator(
        old_material_id,
        f"corpus/live/{old_material_id}.md",
        paragraph=1,
        quote="The synthetic rate is three percent.",
    )
    new_evidence = EvidenceLocator(
        new_material_id,
        f"corpus/live/{new_material_id}.md",
        paragraph=2,
        quote="The synthetic rate is four percent.",
    )
    case = EvolutionCase(case_id, "policy", "Live M1 policy", T0, "active")
    old_fact = TemporalFact(
        "Synthetic Agency",
        "sets rate",
        "3%",
        T0,
        revision_at,
        T0,
        (old_material_id,),
        0.91,
        "source_explicit",
        (old_evidence,),
        old_fact_id,
    )
    new_fact = TemporalFact(
        "Synthetic Agency",
        "sets rate",
        "4%",
        revision_at,
        None,
        revision_at,
        (new_material_id,),
        0.93,
        "source_explicit",
        (new_evidence,),
        new_fact_id,
    )
    supersedes = TemporalRelation(
        supersedes_id,
        "supersedes",
        new_fact_id,
        old_fact_id,
        revision_at,
        None,
        revision_at,
        (new_material_id,),
        (new_evidence,),
        0.93,
        "source_explicit",
    )
    contradicts = TemporalRelation(
        contradicts_id,
        "contradicts",
        new_fact_id,
        old_fact_id,
        revision_at,
        None,
        revision_at,
        (old_material_id, new_material_id),
        (old_evidence, new_evidence),
        0.91,
        "source_explicit",
    )
    materials = (
        Material(
            id=old_material_id,
            title="Live M1 original notice",
            source="example.gov",
            published_at=T0,
            fetched_at=T0,
            type="policy",
            content="Synthetic original notice.",
            original_format="md",
        ),
        Material(
            id=new_material_id,
            title="Live M1 revision notice",
            source="example.gov",
            published_at=revision_at,
            fetched_at=revision_at,
            type="policy",
            content="Synthetic revision notice.",
            original_format="md",
        ),
    )

    def payloads(timeline, kind):
        return {
            json.loads(entry.payload).get(
                "fact_id" if kind == "temporal_fact" else "relation_id"
            ): (entry, json.loads(entry.payload))
            for entry in timeline.entries
            if entry.kind == kind
        }

    def evidence_payload(locator):
        return {
            "source_id": locator.source_id,
            "corpus_path": locator.corpus_path,
            "paragraph": locator.paragraph,
            "page": locator.page,
            "quote": locator.quote,
        }

    async def exercise():
        first = await create_runtime(
            config_path, graphiti_client_factory=factory
        )
        try:
            write = await first.graph.add_case(
                case,
                facts=(old_fact, new_fact),
                relations=(supersedes, contradicts),
                materials=materials,
            )
            assert len(write.added_keys) == 7
            duplicate = await first.graph.add_case(
                case,
                facts=(old_fact, new_fact),
                relations=(supersedes, contradicts),
                materials=materials,
            )
            assert duplicate.added_keys == ()
            assert set(duplicate.skipped_keys) == set(write.added_keys)
            early = await first.graph.timeline(case_id, early_cutoff)
            late = await first.graph.timeline(case_id, late_cutoff)
        finally:
            await first.close()

        early_facts = payloads(early, "temporal_fact")
        assert set(early_facts) == {old_fact_id}
        assert payloads(early, "temporal_relation") == {}
        assert not early.invalidated_entries

        late_facts = payloads(late, "temporal_fact")
        late_relations = payloads(late, "temporal_relation")
        invalidated_facts = {
            json.loads(entry.payload).get("fact_id"): (
                entry,
                json.loads(entry.payload),
            )
            for entry in late.invalidated_entries
            if entry.kind == "temporal_fact"
        }
        assert set(late_facts) == {new_fact_id}
        assert set(invalidated_facts) == {old_fact_id}
        assert set(late_relations) == {supersedes_id, contradicts_id}
        assert late_facts[new_fact_id][0].source_ids == (new_material_id,)
        assert late_facts[new_fact_id][0].evidence == (new_evidence,)
        assert late_facts[new_fact_id][1]["source_ids"] == [new_material_id]
        assert late_facts[new_fact_id][1]["evidence"] == [
            evidence_payload(new_evidence)
        ]
        assert invalidated_facts[old_fact_id][0].source_ids == (old_material_id,)
        assert invalidated_facts[old_fact_id][0].evidence == (old_evidence,)
        assert invalidated_facts[old_fact_id][1]["source_ids"] == [
            old_material_id
        ]
        assert invalidated_facts[old_fact_id][1]["evidence"] == [
            evidence_payload(old_evidence)
        ]
        assert late_relations[supersedes_id][0].source_ids == (new_material_id,)
        assert late_relations[supersedes_id][0].evidence == (new_evidence,)
        assert late_relations[supersedes_id][1]["source_ids"] == [
            new_material_id
        ]
        assert late_relations[supersedes_id][1]["evidence"] == [
            evidence_payload(new_evidence)
        ]
        assert late_relations[contradicts_id][0].source_ids == (
            old_material_id,
            new_material_id,
        )
        assert late_relations[contradicts_id][0].evidence == (
            old_evidence,
            new_evidence,
        )
        assert late_relations[contradicts_id][1]["source_ids"] == [
            old_material_id,
            new_material_id,
        ]
        assert late_relations[contradicts_id][1]["evidence"] == [
            evidence_payload(old_evidence),
            evidence_payload(new_evidence),
        ]

        second = await create_runtime(
            config_path, graphiti_client_factory=factory
        )
        try:
            restarted = await second.graph.timeline(case_id, late_cutoff)
            rewrite = await second.graph.add_case(
                case,
                facts=(old_fact, new_fact),
                relations=(supersedes, contradicts),
                materials=materials,
            )
            assert rewrite.added_keys == ()
            assert set(rewrite.skipped_keys) == set(write.added_keys)
        finally:
            await second.close()

        assert [entry.episode_key for entry in restarted.entries] == [
            entry.episode_key for entry in late.entries
        ]
        assert [entry.episode_key for entry in restarted.invalidated_entries] == [
            entry.episode_key for entry in late.invalidated_entries
        ]
        assert factory.calls == [first.config.graphiti, second.config.graphiti]
        assert factory.llm_clients[0].response_models
        assert factory.llm_clients[1].response_models == []
        assert all(embedder.calls for embedder in factory.embedders)

    run(exercise())
