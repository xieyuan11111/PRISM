"""Composition tests for the automatic event-driven evolution pipeline."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pytest

from prism.config import PrismConfig
from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    TemporalFact,
)
from prism.extraction import ExtractionResult
from prism.graph import GraphEpisode
from prism.pipeline import PipelineError
from prism.runtime import PrismRuntime, create_runtime


CASE_ID = "case-1"
PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

DOC_TEMPLATE = """---
source: example.test
title: {title}
published_at: 2026-08-30T09:00:00+00:00
fetched_at: 2026-08-31T12:00:00+00:00
type: policy
case_tags: ["case-1"]
access_level: {access}
---

{body}
"""


def write_material(home: Path, name: str, body: str, access: str = "fulltext") -> Path:
    corpus = home / "corpus" / "2026-08" / "example.test"
    corpus.mkdir(parents=True, exist_ok=True)
    target = corpus / f"{name}.md"
    target.write_text(
        DOC_TEMPLATE.format(title=name, body=body, access=access),
        encoding="utf-8",
    )
    return target


class FakeGraphBackend:
    """Offline graph storage that dedupes episode keys like a real backend."""

    def __init__(self) -> None:
        self.episodes: dict[str, GraphEpisode] = {}

    async def add_episode(self, episode: GraphEpisode) -> bool:
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        return tuple(self.episodes.values())


class FakeEvolutionExtractor:
    """Deterministic full-extraction stand-in keyed by material.case_tags."""

    name = "fake-evolution"

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.exc = exc

    async def extract(self, material):
        return await self.extract_material(material)

    async def extract_material(self, material, *, corpus_path=None):
        self.calls.append(material.id)
        if self.exc is not None:
            raise self.exc
        case_id = material.case_tags[0] if material.case_tags else None
        if case_id is None:
            return ExtractionResult()
        locator = EvidenceLocator(
            source_id=material.id,
            corpus_path=f"corpus/2026-08/example.test/{material.id}.md",
            paragraph=1,
            quote=material.content,
        )
        case = EvolutionCase(
            case_id=case_id,
            case_type="policy",
            canonical_name="Revised policy",
            start_at=material.published_at,
            status="active",
        )
        node = EvolutionNode(
            id="node-1",
            case_id=case_id,
            node_type="publication",
            happened_at=material.published_at,
            summary=material.title,
            source_ids=(material.id,),
            valid_at=material.published_at,
            observed_at=material.published_at,
            evidence=(locator,),
            provenance_type="explicit",
        )
        fact = TemporalFact(
            subject="Agency",
            predicate="published",
            object=material.title,
            valid_at=material.published_at,
            invalid_at=None,
            observed_at=material.published_at,
            source_ids=(material.id,),
            confidence=0.9,
            provenance_type="explicit",
            evidence=(locator,),
        )
        claim = Claim(
            claim_id="claim-1",
            actor="Agency",
            proposition=f"{material.title} matters.",
            stance="support",
            stated_at=material.published_at,
            based_on=(material.id,),
            evidence=(locator,),
            observed_at=material.published_at,
        )
        return ExtractionResult(
            case=case,
            nodes=(node,),
            temporal_facts=(fact,),
            claims=(claim,),
        )


async def await_completion(runtime: PrismRuntime, material_id: str) -> object:
    for _ in range(500):
        run = runtime.pipeline.run_for(material_id)
        if run is not None:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"pipeline never processed {material_id}")


def run(coro):
    return asyncio.run(coro)


def make_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    PrismConfig().save(config_path)
    return config_path


@pytest.fixture(autouse=True)
def isolated_prism_home(tmp_path, monkeypatch):
    """Never touch the developer's real PRISM home from these tests."""

    monkeypatch.setenv("PRISM_HOME", str(tmp_path))
    return tmp_path


def test_ingest_event_automatically_runs_index_extract_and_merged_graph(tmp_path):
    async def main():
        config_path = make_config(tmp_path)
        backend = FakeGraphBackend()
        extractor = FakeEvolutionExtractor()
        runtime = await create_runtime(
            config_path, graph_backend=backend, extraction_service=extractor
        )
        try:
            document = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            result = await runtime.api.ingest_material(document)

            run = await await_completion(runtime, result.material.id)
            assert run.status == "completed"
            assert [stage.name for stage in run.stages] == [
                "index",
                "extract",
                "graph",
            ]
            assert run.stages[2].status == "written"
            assert "1 accumulated material(s)" in run.stages[2].detail
            assert runtime.dispatch_errors == ()
            assert extractor.calls == [result.material.id]
            assert runtime.case_service.case_for_material(result.material.id) == CASE_ID

            node_episodes = [
                episode
                for episode in backend.episodes.values()
                if episode.kind == "evolution_node"
            ]
            assert len(node_episodes) == 1
            assert f"{result.material.id}::node-1" in node_episodes[0].name
            case_episodes = [
                episode
                for episode in backend.episodes.values()
                if episode.kind == "evolution_case"
            ]
            assert len(case_episodes) == 1
        finally:
            await runtime.close()

    run(main())


def test_two_materials_of_one_case_accumulate_without_case_duplication(tmp_path):
    async def main():
        config_path = make_config(tmp_path)
        backend = FakeGraphBackend()
        runtime = await create_runtime(
            config_path,
            graph_backend=backend,
            extraction_service=FakeEvolutionExtractor(),
        )
        try:
            first_doc = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            second_doc = write_material(
                tmp_path,
                "policy-reaction",
                "Analysts responded to the revised policy today.",
            )
            first = await runtime.api.ingest_material(first_doc)
            second = await runtime.api.ingest_material(second_doc)
            await await_completion(runtime, first.material.id)
            await await_completion(runtime, second.material.id)

            second_run = runtime.pipeline.run_for(second.material.id)
            assert second_run is not None
            assert "2 accumulated material(s)" in second_run.stages[2].detail

            node_episodes = [
                episode
                for episode in backend.episodes.values()
                if episode.kind == "evolution_node"
            ]
            # One scoped node episode per material — never a duplicated node.
            assert len(node_episodes) == 2
            names = {episode.name for episode in node_episodes}
            assert any(f"{first.material.id}::node-1" in name for name in names)
            assert any(f"{second.material.id}::node-1" in name for name in names)

            # The case record is append-only and versioned: the version that
            # declares BOTH accumulated nodes exists...
            case_bodies = [
                episode.episode_body
                for episode in backend.episodes.values()
                if episode.kind == "evolution_case"
            ]
            assert any(
                f"{first.material.id}::node-1" in body
                and f"{second.material.id}::node-1" in body
                for body in case_bodies
            )

            # ...and rewriting the SAME accumulated state is fully deduped:
            episodes_before = len(backend.episodes)
            outcome = await runtime.case_service.merge_case(CASE_ID)
            assert outcome is not None
            assert outcome.material_ids == (first.material.id, second.material.id)
            assert outcome.write.added_keys == ()
            assert len(backend.episodes) == episodes_before
        finally:
            await runtime.close()

    run(main())


def test_accumulated_case_state_rebuilds_after_restart(tmp_path):
    async def main():
        config_path = make_config(tmp_path)
        first_doc = write_material(
            tmp_path, "policy-update", "The agency published the revised policy."
        )
        second_doc = write_material(
            tmp_path,
            "policy-reaction",
            "Analysts responded to the revised policy today.",
        )
        runtime = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=FakeEvolutionExtractor(),
        )
        first = await runtime.api.ingest_material(first_doc)
        second = await runtime.api.ingest_material(second_doc)
        await await_completion(runtime, first.material.id)
        await await_completion(runtime, second.material.id)
        original = await runtime.case_service.merge_case(CASE_ID)
        assert original is not None
        await runtime.close()

        # A restarted runtime over the same PRISM home rebuilds the identical
        # accumulated case from the local ledger alone — no LLM, no network.
        restarted = await create_runtime(config_path, graph_backend=FakeGraphBackend())
        try:
            assert restarted.case_service.case_for_material(first.material.id) == CASE_ID
            rebuilt = await restarted.case_service.merge_case(CASE_ID)
            assert rebuilt is not None
            assert rebuilt.bundle == original.bundle
            assert rebuilt.material_ids == original.material_ids
        finally:
            await restarted.close()

    run(main())


def test_default_offline_runtime_stays_fully_offline(tmp_path):
    async def main():
        runtime = await create_runtime(make_config(tmp_path))
        try:
            document = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            result = await runtime.api.ingest_material(document)
            run = await await_completion(runtime, result.material.id)

            # The offline extractor records why structured extraction was
            # skipped; nothing reaches the graph or the case ledger.
            assert run.status == "completed"
            extract_stage = run.stages[1]
            assert extract_stage.status == "extracted"
            assert "no LLM router configured" in "\n".join(
                extract_stage.result.warnings
            )
            assert run.stages[2].status == "skipped"
            assert "no case" in run.stages[2].detail
            assert runtime.case_service.case_for_material(result.material.id) is None
            assert runtime.dispatch_errors == ()
        finally:
            await runtime.close()

    run(main())


def test_non_fulltext_materials_are_indexed_but_never_extracted(tmp_path):
    async def main():
        runtime = await create_runtime(
            make_config(tmp_path),
            graph_backend=FakeGraphBackend(),
            extraction_service=FakeEvolutionExtractor(),
        )
        try:
            document = write_material(
                tmp_path,
                "abstract-only",
                "Placeholder body of an abstract-only record.",
                access="abstract_only",
            )
            result = await runtime.api.ingest_material(document)
            run = await await_completion(runtime, result.material.id)

            assert [stage.name for stage in run.stages] == [
                "index",
                "extract",
                "graph",
            ]
            assert run.stages[1].status == "skipped"
            assert "abstract_only" in run.stages[1].detail
            assert run.stages[2].status == "skipped"
            assert runtime.case_service.case_for_material(result.material.id) is None
            assert runtime.dispatch_errors == ()
        finally:
            await runtime.close()

    run(main())


def test_subscriber_failures_are_isolated_and_auditable(tmp_path):
    async def main():
        extractor = FakeEvolutionExtractor(exc=RuntimeError("extractor exploded"))
        runtime = await create_runtime(
            make_config(tmp_path),
            graph_backend=FakeGraphBackend(),
            extraction_service=extractor,
        )
        try:
            document = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            result = await runtime.api.ingest_material(document)
            for _ in range(500):
                if runtime.dispatch_errors:
                    break
                await asyncio.sleep(0.01)

            # Ingestion itself succeeded and the failure was isolated to the
            # subscriber, which stays auditable and retryable.
            assert runtime.evidence_store.get(result.material.id) is not None
            assert runtime.pipeline.run_for(result.material.id) is None
            assert len(runtime.dispatch_errors) == 1
            error = runtime.dispatch_errors[0]
            assert "extractor exploded" in str(error.exception)
            assert error.failed_at is not None
            assert error.failed_at.tzinfo is not None

            # The material-level failure audit carries material id, stage,
            # error type and time — never a bare generic error.
            failure = runtime.pipeline.failure_for(result.material.id)
            assert failure is not None
            assert failure.material_id == result.material.id
            assert failure.stage == "extract"
            assert failure.error_type == "RuntimeError"
            assert "extractor exploded" in failure.message
            assert failure.failed_at is not None
            assert failure.failed_at.tzinfo is not None
            # No fake success: nothing reached the ledger or the graph.
            assert runtime.case_service.case_for_material(result.material.id) is None
            assert runtime.case_ledger.entries(CASE_ID) == ()

            extractor.exc = None
            await runtime.api.ingest_material(document)
            run = await await_completion(runtime, result.material.id)
            assert run.status == "completed"
            assert runtime.case_service.case_for_material(result.material.id) == CASE_ID
            # The stale failure audit is cleared by the successful run.
            assert runtime.pipeline.failure_for(result.material.id) is None
        finally:
            await runtime.close()

    run(main())


def test_process_material_waits_synchronously_and_replays_idempotently(tmp_path):
    """process_material is the synchronous path: when the API returns, the
    material's pipeline run AND its accumulated case outcome are done — no
    polling, no background half-state — and a repeated call is an explicit
    idempotent replay that merges nothing twice."""
    async def main():
        config_path = make_config(tmp_path)
        backend = FakeGraphBackend()
        extractor = FakeEvolutionExtractor()
        runtime = await create_runtime(
            config_path,
            graph_backend=backend,
            extraction_service=extractor,
        )
        try:
            document = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            result = await runtime.api.process_material(document)

            material_id = result.material_id
            assert result.replayed is False
            run = runtime.pipeline.run_for(material_id)
            assert run is not None and run.status == "completed"
            assert [stage.name for stage in run.stages] == [
                "index",
                "extract",
                "graph",
            ]
            assert runtime.case_service.case_for_material(material_id) == CASE_ID
            assert [e.material_id for e in runtime.case_ledger.entries(CASE_ID)] == [
                material_id
            ]
            assert result.case_outcome is not None
            assert result.case_outcome.material_ids == (material_id,)
            assert runtime.dispatch_errors == ()
            node_episodes = [
                episode
                for episode in backend.episodes.values()
                if episode.kind == "evolution_node"
            ]
            assert len(node_episodes) == 1

            episodes_before = len(backend.episodes)
            replay = await runtime.api.process_material(material_id)
            assert replay.replayed is True
            assert replay.pipeline is result.pipeline
            assert replay.case_outcome.material_ids == (material_id,)
            # Idempotent: no new execution, no duplicate merge, no graph dupes.
            assert len(backend.episodes) == episodes_before
            assert extractor.calls == [material_id]
        finally:
            await runtime.close()

    run(main())


class GatedEvolutionExtractor(FakeEvolutionExtractor):
    """Deterministic extractor whose first extraction blocks until released."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gated = False

    async def extract_material(self, material, *, corpus_path=None):
        if not self.gated:
            self.gated = True
            self.started.set()
            await self.release.wait()
        return await super().extract_material(material, corpus_path=corpus_path)


def test_plain_ingest_is_async_queued_and_queryable(tmp_path):
    """ingest_material keeps explicit asynchronous semantics: it returns when
    ingestion is done while automatic processing is still in flight, and the
    completed result is queryable — never silently reported as done."""
    async def main():
        extractor = GatedEvolutionExtractor()
        runtime = await create_runtime(
            make_config(tmp_path),
            graph_backend=FakeGraphBackend(),
            extraction_service=extractor,
        )
        try:
            document = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            ingested = await runtime.api.ingest_material(document)
            material_id = ingested.material.id
            # ingest_material does NOT wait for the pipeline: the material is
            # still being processed when the API has already returned.
            await asyncio.wait_for(extractor.started.wait(), timeout=1)
            assert runtime.pipeline.run_for(material_id) is None

            # process_material is the explicit barrier/query: it blocks until
            # the in-flight event-driven run completes (shared lock), then
            # reports the authoritative outcome — never a second execution.
            barrier = asyncio.create_task(
                runtime.api.process_material(material_id)
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not barrier.done()
            extractor.release.set()
            outcome = await asyncio.wait_for(barrier, timeout=5)
            assert outcome.pipeline.status == "completed"
            assert outcome.case_outcome is not None
            assert outcome.case_outcome.material_ids == (material_id,)
            assert outcome.replayed is True
            assert extractor.calls == [material_id]

            # Re-querying after completion is an explicit idempotent replay.
            replay = await runtime.api.process_material(material_id)
            assert replay.replayed is True
            assert replay.pipeline is outcome.pipeline
        finally:
            await runtime.close()

    run(main())


def test_process_material_raises_on_failure_and_recovers_by_retry(tmp_path):
    """The synchronous entry point never fakes success: a failing pipeline is
    raised as the structured PipelineError with stage and material id, the
    outcome is queryable as failed (never committed), and a later call after
    recovery is a safe retry that commits and clears the stale audit."""

    async def main():
        extractor = FakeEvolutionExtractor(exc=RuntimeError("extractor exploded"))
        runtime = await create_runtime(
            make_config(tmp_path),
            graph_backend=FakeGraphBackend(),
            extraction_service=extractor,
        )
        try:
            document = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            result = await runtime.api.ingest_material(document)
            material_id = result.material.id
            for _ in range(500):
                if runtime.pipeline.failure_for(material_id) is not None:
                    break
                await asyncio.sleep(0.01)
            assert runtime.pipeline.failure_for(material_id) is not None

            failed_outcome = runtime.pipeline.outcome_for(material_id)
            assert failed_outcome is not None
            assert failed_outcome.status == "failed"
            assert failed_outcome.stage == "extract"
            assert failed_outcome.error_type == "RuntimeError"
            assert failed_outcome.occurred_at is not None
            assert failed_outcome.occurred_at.tzinfo is not None

            with pytest.raises(PipelineError) as info:
                await runtime.api.process_material(material_id)
            assert info.value.stage == "extract"
            assert info.value.material_id == material_id
            # Still no fake success: no completed run, no ledger row.
            assert runtime.pipeline.run_for(material_id) is None
            assert runtime.case_ledger.entries(CASE_ID) == ()

            # Recovery is a safe retry: the material is reprocessed (never a
            # claimed replay) and the stale failed audit is cleared.
            extractor.exc = None
            retried = await runtime.api.process_material(material_id)
            assert retried.replayed is False
            assert retried.pipeline.status == "completed"
            assert runtime.pipeline.outcome_for(material_id).status == "committed"
            assert runtime.pipeline.failure_for(material_id) is None
            assert runtime.case_service.case_for_material(material_id) == CASE_ID
        finally:
            await runtime.close()

    run(main())


def test_failed_outcome_audit_survives_restart_and_retry_commits(tmp_path):
    """Terminal outcome records are persisted locally: a failure stays
    queryable after a restart, while the completed-run registry is honestly
    per-process.  A fresh-process process_material is a genuine retry that
    executes again and moves the material to committed."""

    async def main():
        config_path = make_config(tmp_path)
        failing = FakeEvolutionExtractor(exc=RuntimeError("extractor exploded"))
        first = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=failing,
        )
        document = write_material(
            tmp_path, "policy-update", "The agency published the revised policy."
        )
        result = await first.api.ingest_material(document)
        material_id = result.material.id
        for _ in range(500):
            if first.pipeline.failure_for(material_id) is not None:
                break
            await asyncio.sleep(0.01)
        assert first.pipeline.outcome_for(material_id).status == "failed"
        await first.close()

        restarted = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=FakeEvolutionExtractor(),
        )
        try:
            outcome = restarted.pipeline.outcome_for(material_id)
            assert outcome is not None
            assert outcome.status == "failed"
            assert outcome.stage == "extract"
            assert outcome.error_type == "RuntimeError"
            assert outcome.occurred_at is not None
            assert outcome.occurred_at.tzinfo is not None
            # The completed-run registry is per-process and honestly empty.
            assert restarted.pipeline.run_for(material_id) is None

            outcome_entries = restarted.pipeline_outcome_ledger.entries()
            assert any(
                entry.material_id == material_id and entry.status == "failed"
                for entry in outcome_entries
            )

            retried = await restarted.api.process_material(material_id)
            assert retried.replayed is False
            assert retried.pipeline.status == "completed"
            assert restarted.pipeline.outcome_for(material_id).status == "committed"
            assert restarted.pipeline.failure_for(material_id) is None
        finally:
            await restarted.close()

    run(main())


def test_cross_process_rebinding_is_refused_as_a_typed_conflict(tmp_path):
    """One material binds one case across processes too: a fresh process whose
    re-extraction now declares a different case is refused with the typed
    MaterialCaseConflict before any row or graph write — the durable binding
    stays intact and nothing is silently re-bound."""

    async def main():
        from dataclasses import replace

        from prism.cases.ledger import MaterialCaseConflict

        config_path = make_config(tmp_path)
        first = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=FakeEvolutionExtractor(),
        )
        document = write_material(
            tmp_path, "policy-update", "The agency published the revised policy."
        )
        result = await first.api.ingest_material(document)
        material_id = result.material.id
        await await_completion(first, material_id)
        assert first.case_service.case_for_material(material_id) == CASE_ID
        await first.close()

        class RebindingExtractor(FakeEvolutionExtractor):
            async def extract_material(self, material, *, corpus_path=None):
                rebound = replace(material, case_tags=("case-2",))
                return await super().extract_material(
                    rebound, corpus_path=corpus_path
                )

        restarted = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=RebindingExtractor(),
        )
        try:
            with pytest.raises(MaterialCaseConflict) as info:
                await restarted.api.process_material(material_id)
            assert info.value.material_id == material_id
            assert info.value.case_ids == (CASE_ID,)
            assert info.value.attempted_case == "case-2"

            # The refusal happened before any write: no case-2 row, the
            # durable case-1 binding unchanged, no completed run, and the
            # failure is auditable with the typed error.
            assert restarted.case_ledger.entries("case-2") == ()
            assert restarted.case_service.case_for_material(material_id) == CASE_ID
            assert restarted.pipeline.run_for(material_id) is None
            failure = restarted.pipeline.failure_for(material_id)
            assert failure is not None
            assert failure.error_type == "MaterialCaseConflict"
            assert failure.stage == "graph"
            outcome = restarted.pipeline.outcome_for(material_id)
            assert outcome is not None
            assert outcome.status == "failed"
            assert outcome.error_type == "MaterialCaseConflict"
        finally:
            await restarted.close()

    run(main())


def test_runtime_case_binding_cannot_add_ambiguity(tmp_path):
    """A second accumulation attempt under a different case is refused before
    it can bind anything; the ledger keeps exactly one case per material."""
    async def main():
        from dataclasses import replace

        from prism.cases.ledger import MaterialCaseConflict

        runtime = await create_runtime(
            make_config(tmp_path),
            graph_backend=FakeGraphBackend(),
            extraction_service=FakeEvolutionExtractor(),
        )
        try:
            document = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            result = await runtime.api.ingest_material(document)
            await await_completion(runtime, result.material.id)
            material_id = result.material.id
            assert runtime.case_service.case_for_material(material_id) == CASE_ID

            # Re-extract the SAME material, but the extraction now declares a
            # different case: the automatic path must refuse the re-binding.
            from prism.pipeline.resolver import material_from_entry

            entry = runtime.evidence_store.get(material_id)
            assert entry is not None
            bound = material_from_entry(entry)
            foreign = replace(bound, case_tags=("case-2",))
            extractor = FakeEvolutionExtractor()
            extraction = await extractor.extract_material(foreign)
            assert extraction.case is not None
            assert extraction.case.case_id == "case-2"

            with pytest.raises(MaterialCaseConflict) as info:
                await runtime.case_service.record_extraction(foreign, extraction)
            assert info.value.material_id == material_id
            assert info.value.case_ids == (CASE_ID,)
            assert info.value.attempted_case == "case-2"
            assert runtime.case_service.case_ids_for_material(material_id) == (
                CASE_ID,
            )
            assert runtime.case_ledger.entries("case-2") == ()
        finally:
            await runtime.close()

    run(main())


def test_close_unsubscribes_the_pipeline_and_is_idempotent(tmp_path):
    async def main():
        runtime = await create_runtime(
            make_config(tmp_path), graph_backend=FakeGraphBackend()
        )
        subscription_id = runtime.pipeline_subscription_id
        assert subscription_id is not None
        await runtime.close()
        await runtime.close()  # idempotent
        # close() already removed the subscription: a late unsubscribe
        # finds nothing, so the pipeline subscriber never outlives the bus.
        assert await runtime.event_bus.unsubscribe(subscription_id) is False

    run(main())


def test_cli_process_and_merge_case_match_the_api_surface(tmp_path):
    """CLI and API agree on the automatic pipeline (default offline runtime)."""

    async def main():
        from prism.cli import main as cli_main

        document = write_material(
            tmp_path, "policy-update", "The agency published the revised policy."
        )
        out, err = StringIO(), StringIO()
        status = await cli_main(
            ["ingest", str(document), "--process"], stdout=out, stderr=err
        )
        assert status == 0, err.getvalue()
        payload = json.loads(out.getvalue())
        assert payload["pipeline"]["status"] == "completed"
        assert payload["pipeline"]["stages"][2]["status"] == "skipped"
        # Offline default: no LLM router, so no case is fabricated.
        assert payload["case_id"] is None
        assert payload["replayed"] is False
        warnings = " ".join(payload["warnings"])
        assert "no LLM router configured" in warnings
        material_id = payload["material_id"]

        out, err = StringIO(), StringIO()
        status = await cli_main(["process", material_id], stdout=out, stderr=err)
        assert status == 0, err.getvalue()
        replay = json.loads(out.getvalue())
        assert replay["material_id"] == material_id
        # A fresh CLI process starts a fresh runtime, so this is an honest
        # deterministic re-run (never a duplicate write): the completed run is
        # reported with the same truthful shape.
        assert replay["pipeline"]["status"] == "completed"
        assert replay["replayed"] is False
        assert replay["case_id"] is None

        out, err = StringIO(), StringIO()
        status = await cli_main(
            ["merge-case", "case-1"], stdout=out, stderr=err
        )
        assert status == 1
        assert "no accumulated extractions" in err.getvalue()

    run(main())


def test_cli_ingest_fails_the_command_when_automatic_processing_fails(
    tmp_path, monkeypatch
):
    """A background subscriber failure never exits 0 as if processing had
    succeeded: the owned CLI reports the auditable dispatch failure."""

    async def main():
        from prism.cli import main as cli_main
        from prism.runtime import create_runtime as real_create

        config_path = make_config(tmp_path)
        document = write_material(
            tmp_path, "policy-update", "The agency published the revised policy."
        )

        async def failing_runtime():
            return await real_create(
                config_path,
                graph_backend=FakeGraphBackend(),
                extraction_service=FakeEvolutionExtractor(
                    exc=RuntimeError("extractor exploded")
                ),
            )

        monkeypatch.setattr("prism.runtime.create_runtime", failing_runtime)
        out, err = StringIO(), StringIO()
        status = await cli_main(["ingest", str(document)], stdout=out, stderr=err)
        assert status == 1
        payload = json.loads(err.getvalue())
        assert payload["error"]["type"] == "DispatchError"
        assert "extractor exploded" in payload["error"]["message"]
        assert "material" in payload["error"]["message"]
        assert out.getvalue() == ""

    run(main())
