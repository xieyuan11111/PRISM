"""Persistent offline graph backend tests (CLI default runtime).

The default runtime (``graphiti.enabled=false``, no injected graph backend)
must persist :class:`~prism.graph.GraphEpisode` across CLI processes through
the project-owned SQLite episode registry, so ``prism timeline`` /
``state`` / ``snapshot`` / ``compare`` / ``report`` in a NEW process read
what ``prism process`` wrote.  What the tests pin down:

* the default composition owns a ``SQLiteEpisodeRegistry`` with
  ``database == "offline"`` plus a ``SQLiteOfflineGraphBackend`` (never a
  Graphiti backend, never the process-local ``OfflineGraphBackend``) and
  closes the registry on runtime shutdown;
* write in runtime A -> close -> timeline/state/snapshot/compare/report in
  runtime B read the episodes back (no ``empty_timeline`` report);
* a duplicate write across the restart is skipped (GraphService reports it
  under ``skipped_keys`` and the row count stays at one);
* episodes recorded under a foreign group (a Graphiti group in the shared
  table) are invisible to the offline group and vice versa, and the SAME
  deterministic episode_key coexists independently in both groups: a
  foreign row neither suppresses the write (either direction) nor leaks
  into the other group's search;
* the Graphiti-enabled composition path is NOT replaced: it still composes
  a real :class:`~prism.graph.GraphitiBackend` with its own registry;
* an old EvidenceStore-only database migrates additively through the
  default runtime, with its rows untouched;
* unreadable (tampered/truncated) rows fail closed: they are skipped with
  an auditable warning instead of being guessed back into the timeline.

Nothing here imports graphiti-core or neo4j and nothing touches a network.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prism.config import GraphitiConfig, PathConfig, PrismConfig
from prism.domain import EvolutionCase, EvidenceLocator
from prism.graph import (
    GraphEpisode,
    GraphitiBackend,
    SQLiteEpisodeRegistry,
    SQLiteOfflineGraphBackend,
)
from prism.analyzer.models import GAP_EMPTY_TIMELINE
from prism.runtime import create_runtime
from prism.store import EvidenceStore

from graphiti_fakes import FakeGraphitiClient

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=1)

LEGACY_ROW = (
    "mat-legacy",
    "Legacy title",
    "example.gov",
    "2026-08-01T12:00:00+00:00",
    "2026-08-01T12:00:00+00:00",
    "policy",
    "corpus/legacy.md",
    "Legacy body text.",
    "hash-legacy",
    "2026-08-01T12:00:00+00:00",
)


def run(coro):
    return asyncio.run(coro)


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        raw_dir=Path("raw"),
        corpus_dir=Path("corpus"),
    )


def make_evidence() -> tuple[EvidenceLocator, ...]:
    return (
        EvidenceLocator(
            source_id="material-a",
            corpus_path="corpus/doc-a.md",
            paragraph=1,
            quote="The policy was published.",
        ),
    )


def make_episode(
    key: str,
    *,
    case_id: str = "case-a",
    kind: str = "claim",
    proposition: str = "The strain tolerates heat stress.",
) -> GraphEpisode:
    """A PRISM-schema episode whose payload supports timeline summaries."""
    payload: dict[str, object] = {
        "schema": "prism.graph.episode.v2",
        "case_id": case_id,
        "kind": kind,
        "reference_time": NOW.isoformat(),
        "valid_at": NOW.isoformat(),
        "invalid_at": None,
        "source_ids": ["material-a"],
        "evidence": [
            {
                "source_id": item.source_id,
                "corpus_path": item.corpus_path,
                "paragraph": item.paragraph,
                "page": item.page,
                "quote": item.quote,
            }
            for item in make_evidence()
        ],
        "episode_key": key,
    }
    if kind == "claim":
        payload["claim_id"] = "claim-a"
        payload["proposition"] = proposition
    if kind == "evolution_case":
        payload["logical_key"] = f"case:{case_id}"
        payload["canonical_name"] = "Heat tolerant strains"
        payload["status"] = "open"
    return GraphEpisode(
        episode_key=key,
        name=f"prism:{case_id}:{kind}:{key[:12]}",
        case_id=case_id,
        kind=kind,
        episode_body=json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        reference_time=NOW,
        valid_at=NOW,
        invalid_at=None,
        source_ids=("material-a",),
        evidence=make_evidence(),
    )


def case_episode(key: str, case_id: str = "case-a") -> GraphEpisode:
    return make_episode(key, case_id=case_id, kind="evolution_case")


def claim_episode(key: str, case_id: str = "case-a") -> GraphEpisode:
    return make_episode(key, case_id=case_id, kind="claim")


def test_default_offline_runtime_owns_and_closes_persistent_registry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))

    async def exercise():
        runtime = await create_runtime()
        try:
            assert isinstance(runtime.graph_backend, SQLiteOfflineGraphBackend)
            assert runtime.graphiti_backend is None
            registry = runtime.graph_episode_registry
            assert isinstance(registry, SQLiteEpisodeRegistry)
            assert registry.database == "offline"
            assert registry.closed is False
            assert registry.db_path == (
                tmp_path / "home" / "data" / "index.db"
            ).resolve()
        finally:
            await runtime.close()
        assert registry.closed is True

    run(exercise())


def test_episode_written_in_runtime_a_is_read_back_in_runtime_b(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))

    async def exercise():
        first = await create_runtime()
        try:
            assert await first.graph_backend.add_episode(
                case_episode("cccccccc-1111-2222-3333-444444444444")
            ) is True
            assert await first.graph_backend.add_episode(
                claim_episode("dddddddd-1111-2222-3333-444444444444")
            ) is True
        finally:
            await first.close()

        second = await create_runtime()
        try:
            timeline = await second.graph.timeline("case-a", LATER)
            assert [entry.kind for entry in timeline.entries] == [
                "claim",
                "evolution_case",
            ]
            assert timeline.entries[0].summary == "The strain tolerates heat stress."

            state = await second.analyzer_service.state("case-a", LATER)
            assert len(state.interpretations) == 1

            snapshot = await second.analyzer_service.snapshot("case-a", LATER)
            assert len(snapshot.interpretations) == 1

            comparison = await second.analyzer_service.compare(
                "case-a", NOW - timedelta(days=1), LATER
            )
            assert comparison.case_id == "case-a"
            assert len(comparison.added) >= 1

            document = await second.api.report_case(
                "case-a", LATER, use_llm=False
            )
            assert all(
                gap.gap_type != GAP_EMPTY_TIMELINE for gap in document.evidence_gaps
            )
            assert "The strain tolerates heat stress." in document.markdown
        finally:
            await second.close()

    run(exercise())


def test_duplicate_write_across_restart_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    key = "eeeeeeee-1111-2222-3333-444444444444"

    async def exercise():
        first = await create_runtime()
        try:
            assert await first.graph_backend.add_episode(claim_episode(key)) is True
            case = EvolutionCase(
                case_id="case-a",
                case_type="policy",
                canonical_name="Heat tolerant strains",
                start_at=NOW,
                status="open",
            )
            first_result = await first.graph.add_case(case)
            assert len(first_result.added_keys) == 1
        finally:
            await first.close()

        second = await create_runtime()
        try:
            # The duplicate write across the restart is a no-op...
            assert await second.graph_backend.add_episode(claim_episode(key)) is False
            results = await second.graph_backend.search("prism query")
            assert sorted(
                episode.episode_key for episode in results
            ) == sorted({key, *first_result.added_keys})

            # ...and replaying the same case bundle is reported as skipped,
            # never re-added.
            rerun = await second.graph.add_case(case)
            assert rerun.added_keys == ()
            assert rerun.skipped_keys == first_result.added_keys
        finally:
            await second.close()

    run(exercise())


def test_offline_group_never_reads_or_pollutes_a_graphiti_group(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    offline_key = "aaaaaaaa-1111-2222-3333-444444444444"
    graphiti_key = "bbbbbbbb-1111-2222-3333-444444444444"

    async def exercise():
        runtime = await create_runtime()
        try:
            assert await runtime.graph_backend.add_episode(
                claim_episode(offline_key)
            ) is True
            # A row recorded under a Graphiti group in the SAME shared table
            # (as the enabled path would leave behind) is invisible to the
            # offline group.
            registry = runtime.graph_episode_registry
            registry.put(claim_episode(graphiti_key), group_id="neo4j")
        finally:
            await runtime.close()

        reopened = await create_runtime()
        try:
            results = await reopened.graph_backend.search("prism query")
            assert [episode.episode_key for episode in results] == [offline_key]
            assert reopened.graph_episode_registry.get(graphiti_key) is not None
        finally:
            await reopened.close()

    run(exercise())


def test_same_key_in_a_graphiti_group_does_not_block_the_offline_write(tmp_path):
    # Review-finding regression: a deterministic episode_key recorded under a
    # live Graphiti group (sharing the same SQLite file) must never make the
    # offline group-scoped existence lookup skip its own write.
    paths = make_paths(tmp_path)
    key = "aaaaaaaa-1111-2222-3333-444444444444"
    graphiti_uuid = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"

    graphiti = SQLiteEpisodeRegistry(paths, database="neo4j")
    try:
        graphiti.put(
            claim_episode(key), group_id="neo4j", graphiti_uuid=graphiti_uuid
        )
    finally:
        graphiti.close()

    offline_registry = SQLiteEpisodeRegistry(paths, database="offline")
    backend = SQLiteOfflineGraphBackend(offline_registry)
    try:
        assert run(_add(backend, claim_episode(key))) is True
        results = run(backend.search("prism query"))
        assert [episode.episode_key for episode in results] == [key]
    finally:
        offline_registry.close()

    # Both rows coexist in one database file, each scoped to its own group.
    reopened = SQLiteEpisodeRegistry(paths, database="offline")
    try:
        assert reopened.get(key, group_id="offline") == claim_episode(key)
        assert reopened.get(key, group_id="neo4j") == claim_episode(key)
        assert (
            reopened.get_by_graphiti_uuid(graphiti_uuid, group_id="neo4j")
            == claim_episode(key)
        )
    finally:
        reopened.close()


def test_offline_row_never_blocks_or_leaks_into_the_graphiti_group(tmp_path):
    # The mirror direction: an offline row holding a deterministic key must
    # not suppress the Graphiti group's write of the same key, and each
    # side's search only ever returns its own group's episode body.
    paths = make_paths(tmp_path)
    key = "bbbbbbbb-1111-2222-3333-444444444444"
    ours = claim_episode(key)
    theirs = make_episode(key, proposition="Graphiti-side variant.")

    offline_registry = SQLiteEpisodeRegistry(paths, database="offline")
    backend = SQLiteOfflineGraphBackend(offline_registry)
    try:
        assert run(_add(backend, ours)) is True
    finally:
        offline_registry.close()

    client = FakeGraphitiClient()
    graphiti_registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    graphiti = GraphitiBackend(
        client,
        group_id="neo4j",
        episode_type_json="json",
        registry=graphiti_registry,
    )
    try:
        # The write-before existence lookup is scoped to "neo4j": the offline
        # row does not suppress the Graphiti write (the old unscoped lookup
        # returned False here and skipped the client entirely).
        assert run(graphiti.add_episode(theirs)) is True
        assert len(client.add_calls) == 1
        results = run(graphiti.search("prism query"))
        assert [episode.episode_key for episode in results] == [key]
        assert "Graphiti-side variant." in results[0].episode_body
    finally:
        graphiti_registry.close()

    reopened = SQLiteEpisodeRegistry(paths, database="offline")
    offline_backend = SQLiteOfflineGraphBackend(reopened)
    try:
        results = run(offline_backend.search("prism query"))
        assert [episode.episode_key for episode in results] == [key]
        assert "The strain tolerates heat stress." in results[0].episode_body
        # Idempotency still holds within the offline group itself...
        assert run(_add(offline_backend, ours)) is False
        # ...and the same key exists independently under both groups.
        assert reopened.get(key, group_id="offline") == ours
        assert reopened.get(key, group_id="neo4j") == theirs
    finally:
        reopened.close()


def test_graphiti_enabled_path_still_composes_graphiti_backend(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    config = PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri="bolt://prism-graphiti-spike:7688",
            database="neo4j",
            group_id="neo4j",
            password_env="PRISM_GRAPHITI_PASSWORD",
        )
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)
    graphiti_key = "cccccccc-1111-2222-3333-444444444444"

    async def exercise():
        runtime = await create_runtime(
            config_path, graphiti_client_factory=lambda _config: FakeGraphitiClient()
        )
        try:
            assert isinstance(runtime.graph_backend, GraphitiBackend)
            assert not isinstance(runtime.graph_backend, SQLiteOfflineGraphBackend)
            assert runtime.graph_episode_registry.database == "neo4j"
            assert await runtime.graph_backend.add_episode(
                claim_episode(graphiti_key)
            ) is True
        finally:
            await runtime.close()

        # The default (disabled) runtime on the same PRISM home shares the
        # database file but only ever sees its own "offline" group.
        default_runtime = await create_runtime()
        try:
            results = await default_runtime.graph_backend.search("prism query")
            assert results == ()
        finally:
            await default_runtime.close()

    run(exercise())


def test_default_runtime_migrates_old_evidence_store_database_additively(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    # A database created by an older PRISM version: EvidenceStore tables and
    # one document row, no registry table.
    paths = make_paths(home)
    store = EvidenceStore(paths)
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO documents (source_id, title, source, published_at,"
            " fetched_at, type, path, content, content_hash, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            LEGACY_ROW,
        )
    store.close()

    key = "dddddddd-1111-2222-3333-444444444444"

    async def exercise():
        runtime = await create_runtime()
        try:
            assert await runtime.graph_backend.add_episode(claim_episode(key)) is True
        finally:
            await runtime.close()

        reopened = await create_runtime()
        try:
            results = await reopened.graph_backend.search("prism query")
            assert [episode.episode_key for episode in results] == [key]
        finally:
            await reopened.close()

    run(exercise())

    # The legacy document row survived the additive migration untouched.
    legacy = EvidenceStore(paths)
    try:
        assert legacy.get("mat-legacy") is not None
    finally:
        legacy.close()


def test_registry_list_episodes_is_stable_sorted_and_group_scoped(tmp_path):
    paths = make_paths(tmp_path)
    registry = SQLiteEpisodeRegistry(paths, database="offline")
    try:
        later_case = make_episode(
            "eeeeeeee-1111-2222-3333-444444444444",
            case_id="case-b",
            kind="evolution_case",
        )
        registry.put(later_case, group_id="offline")
        registry.put(claim_episode("aaaaaaaa-1111-2222-3333-444444444444"), group_id="offline")
        registry.put(case_episode("bbbbbbbb-1111-2222-3333-444444444444"), group_id="offline")
        registry.put(
            claim_episode("cccccccc-1111-2222-3333-444444444444", case_id="case-b"),
            group_id="neo4j",
        )

        episodes = registry.list_episodes(group_id="offline")
        assert [episode.episode_key for episode in episodes] == [
            "aaaaaaaa-1111-2222-3333-444444444444",
            "bbbbbbbb-1111-2222-3333-444444444444",
            "eeeeeeee-1111-2222-3333-444444444444",
        ]
    finally:
        registry.close()


def test_bad_registry_row_is_skipped_fail_closed_with_warning(tmp_path):
    paths = make_paths(tmp_path)
    registry = SQLiteEpisodeRegistry(paths, database="offline")
    try:
        registry.put(claim_episode("aaaaaaaa-1111-2222-3333-444444444444"), group_id="offline")
        registry.put(claim_episode("bbbbbbbb-1111-2222-3333-444444444444"), group_id="offline")
        with sqlite3.connect(registry.db_path) as conn:
            conn.execute(
                "UPDATE graphiti_episode_registry SET reference_time = 'not-a-date'"
                " WHERE episode_key = ?",
                ("bbbbbbbb-1111-2222-3333-444444444444",),
            )
    finally:
        registry.close()

    reopened = SQLiteEpisodeRegistry(paths, database="offline")
    backend = SQLiteOfflineGraphBackend(reopened)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            episodes = reopened.list_episodes(group_id="offline")
        assert [episode.episode_key for episode in episodes] == [
            "aaaaaaaa-1111-2222-3333-444444444444"
        ]
        assert any("bbbbbbbb-1111-2222-3333-444444444444" in str(item.message) for item in caught)

        # The backend read path fails closed the same way.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            results = run(backend.search("prism query"))
        assert [episode.episode_key for episode in results] == [
            "aaaaaaaa-1111-2222-3333-444444444444"
        ]
        assert caught
    finally:
        reopened.close()


def test_offline_backend_round_trip_and_idempotency_across_reopen(tmp_path):
    paths = make_paths(tmp_path)
    key = "aaaaaaaa-1111-2222-3333-444444444444"

    first_registry = SQLiteEpisodeRegistry(paths, database="offline")
    first = SQLiteOfflineGraphBackend(first_registry)
    added = run(_add(first, claim_episode(key)))
    assert added is True
    assert run(_add(first, claim_episode(key))) is False
    first_registry.close()

    second_registry = SQLiteEpisodeRegistry(paths, database="offline")
    second = SQLiteOfflineGraphBackend(second_registry)
    try:
        assert run(_add(second, claim_episode(key))) is False
        results = run(second.search("prism query"))
        assert results == (claim_episode(key),)
    finally:
        second_registry.close()


async def _add(backend: SQLiteOfflineGraphBackend, episode: GraphEpisode) -> bool:
    return await backend.add_episode(episode)
