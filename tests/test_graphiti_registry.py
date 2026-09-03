"""Offline tests for the project-owned SQLiteEpisodeRegistry (Phase B).

The registry is PRISM's own durable SQLite persistence for the PRISM
``episode_key`` -> real Graphiti-assigned uuid mapping.  It lives in the same
database file as the EvidenceStore index (``index.db`` under the data dir),
adds its table additively (``CREATE TABLE IF NOT EXISTS``) so databases
created by older PRISM versions migrate in place, and never stores
credentials, hosts or absolute paths.  Nothing here imports graphiti-core or
neo4j and nothing touches a network.

What the tests pin down:

* put/get round trips survive close + reopen (the "restart" the live Phase B
  path depends on), including the canonical episode body, name and evidence;
* reverse lookup by the real Graphiti uuid works after reopen and is scoped
  to the group the row was recorded under (group/database boundary);
* an add whose client result carried no uuid records the episode WITHOUT a
  fabricated uuid: PRISM-key readback keeps working, reverse lookup does not;
* put is an upsert keyed by episode_key (second writes never duplicate rows);
* an old EvidenceStore database (``documents``/``document_fts`` only, with
  rows) migrates additively: its schema and rows survive, and the registry
  table appears on first use;
* close is idempotent and operations after close fail loudly instead of
  silently resurrecting state after the runtime shut the registry down.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prism.config import PathConfig
from prism.domain import EvidenceLocator
from prism.graph import GraphEpisode, GraphEpisodeRegistry, SQLiteEpisodeRegistry
from prism.graph.models import EPISODE_SCHEMA, canonical_json
from prism.store import EvidenceStore

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

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
        EvidenceLocator(
            source_id="material-a",
            corpus_path="corpus/doc-a.md",
            page=2,
        ),
    )


def make_episode(
    key: str = "4d8fe701-5578-5ca3-a436-1f24d29c6300",
    *,
    case_id: str = "case-a",
    kind: str = "claim",
    invalid_at: datetime | None = None,
    source_ids: tuple[str, ...] = ("material-a",),
    evidence: tuple[EvidenceLocator, ...] = (),
    confidence: float | None = None,
    provenance_type: str | None = None,
    evidence_role: str | None = None,
    cited_source_ref: str | None = None,
) -> GraphEpisode:
    payload: dict[str, object] = {
        "schema": EPISODE_SCHEMA,
        "case_id": case_id,
        "kind": kind,
        "reference_time": NOW.isoformat(),
        "valid_at": NOW.isoformat(),
        "invalid_at": invalid_at.isoformat() if invalid_at else None,
        "source_ids": list(source_ids),
        "evidence": [
            {
                "source_id": item.source_id,
                "corpus_path": item.corpus_path,
                "paragraph": item.paragraph,
                "page": item.page,
                "quote": item.quote,
            }
            for item in evidence
        ],
        "episode_key": key,
    }
    if confidence is not None:
        payload["confidence"] = confidence
    if provenance_type is not None:
        payload["provenance_type"] = provenance_type
    if evidence_role is not None:
        payload["evidence_role"] = evidence_role
    if cited_source_ref is not None:
        payload["cited_source_ref"] = cited_source_ref
    return GraphEpisode(
        episode_key=key,
        name=f"prism:{case_id}:{kind}:{key[:12]}",
        case_id=case_id,
        kind=kind,
        episode_body=canonical_json(payload),
        reference_time=NOW,
        valid_at=NOW,
        invalid_at=invalid_at,
        source_ids=source_ids,
        confidence=confidence,
        provenance_type=provenance_type,
        evidence=evidence,
        evidence_role=evidence_role,
        cited_source_ref=cited_source_ref,
    )


def full_episode() -> GraphEpisode:
    """An episode exercising every persisted field."""
    return make_episode(
        "aaaaaaaa-1111-2222-3333-444444444444",
        case_id="case-full",
        kind="temporal_fact",
        invalid_at=NOW + timedelta(days=3),
        source_ids=("material-a", "material-b"),
        evidence=make_evidence(),
        confidence=0.85,
        provenance_type="document",
        evidence_role="cited_prior_research",
        cited_source_ref="Smith et al. (2020)",
    )


@pytest.fixture
def paths(tmp_path):
    return make_paths(tmp_path)


def table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_put_get_round_trip_survives_close_and_reopen(tmp_path):
    paths = make_paths(tmp_path)
    episode = full_episode()

    first = SQLiteEpisodeRegistry(paths, database="neo4j")
    first.put(
        episode,
        group_id="neo4j",
        graphiti_uuid="11111111-2222-3333-4444-555555555555",
    )
    # put is durable immediately (own SQLite transaction), and the row records
    # audit timestamps that parse as UTC ISO-8601.
    with sqlite3.connect(first.db_path) as conn:
        row = conn.execute(
            "SELECT created_at, updated_at, group_id, database"
            " FROM graphiti_episode_registry WHERE episode_key = ?",
            (episode.episode_key,),
        ).fetchone()
        for value in (row[0], row[1]):
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None and parsed.utcoffset() is not None
        assert row[2] == "neo4j"
        assert row[3] == "neo4j"
    first.close()

    # Reopen (the "restart" of the live Phase B path): the same database file
    # must return the identical episode, every field included.
    second = SQLiteEpisodeRegistry(paths, database="neo4j")
    try:
        rebuilt = second.get(episode.episode_key)
        assert rebuilt is not None
        assert rebuilt == episode
        assert rebuilt.episode_body == episode.episode_body
        assert rebuilt.evidence == episode.evidence
    finally:
        second.close()


def test_reverse_lookup_by_real_graphiti_uuid_works_after_reopen(tmp_path):
    paths = make_paths(tmp_path)
    episode = make_episode()
    uuid = "22222222-3333-4444-5555-666666666666"

    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    registry.put(episode, group_id="neo4j", graphiti_uuid=uuid)
    registry.close()

    reopened = SQLiteEpisodeRegistry(paths, database="neo4j")
    try:
        assert reopened.get_by_graphiti_uuid(uuid, group_id="neo4j") == episode
        assert reopened.get_by_graphiti_uuid("unknown-uuid", group_id="neo4j") is None
    finally:
        reopened.close()


def test_reverse_lookup_is_scoped_to_the_recorded_group(tmp_path):
    paths = make_paths(tmp_path)
    ours = make_episode("aaaaaaaa-1111-2222-3333-444444444444", case_id="case-a")
    theirs = make_episode("bbbbbbbb-1111-2222-3333-444444444444", case_id="case-b")
    shared_uuid = "33333333-4444-5555-6666-777777777777"

    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    try:
        # One row for our group and one row for a foreign group that happens
        # to reference the same uuid: a reverse lookup must only ever see the
        # row of the group it asks for (defensive group/database boundary).
        registry.put(ours, group_id="neo4j", graphiti_uuid=shared_uuid)
        registry.put(theirs, group_id="tenant-b", graphiti_uuid=shared_uuid)

        assert registry.get_by_graphiti_uuid(shared_uuid, group_id="neo4j") == ours
        assert (
            registry.get_by_graphiti_uuid(shared_uuid, group_id="tenant-b")
            == theirs
        )
        assert (
            registry.get_by_graphiti_uuid(shared_uuid, group_id="tenant-c")
            is None
        )
        # A uuid recorded in a foreign group is invisible to our group even
        # when the key is unknown.
        foreign_only = "99999999-aaaa-bbbb-cccc-dddddddddddd"
        registry.put(
            make_episode("cccccccc-1111-2222-3333-444444444444"),
            group_id="tenant-b",
            graphiti_uuid=foreign_only,
        )
        assert (
            registry.get_by_graphiti_uuid(foreign_only, group_id="neo4j") is None
        )
    finally:
        registry.close()


def test_uuidless_put_keeps_key_readback_and_never_matches_reverse(tmp_path):
    paths = make_paths(tmp_path)
    episode = make_episode()

    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    try:
        registry.put(episode, group_id="neo4j", graphiti_uuid=None)
        # PRISM-key readback keeps working without any uuid...
        assert registry.get(episode.episode_key) == episode
        # ...but no uuid was fabricated, so reverse lookup cannot match it.
        assert registry.get_by_graphiti_uuid(episode.episode_key, group_id="neo4j") is None
        with sqlite3.connect(registry.db_path) as conn:
            row = conn.execute(
                "SELECT graphiti_uuid FROM graphiti_episode_registry"
                " WHERE episode_key = ?",
                (episode.episode_key,),
            ).fetchone()
        assert row[0] is None
    finally:
        registry.close()


def test_put_is_an_upsert_keyed_by_episode_key(tmp_path):
    paths = make_paths(tmp_path)
    episode = make_episode()

    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    try:
        registry.put(episode, group_id="neo4j", graphiti_uuid="uuid-one")
        registry.put(episode, group_id="neo4j", graphiti_uuid="uuid-two")
        with sqlite3.connect(registry.db_path) as conn:
            rows = conn.execute(
                "SELECT graphiti_uuid FROM graphiti_episode_registry"
                " WHERE episode_key = ?",
                (episode.episode_key,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "uuid-two"
        assert registry.get_by_graphiti_uuid("uuid-two", group_id="neo4j") == episode
        assert registry.get_by_graphiti_uuid("uuid-one", group_id="neo4j") is None
    finally:
        registry.close()


def test_old_evidence_store_database_migrates_additively_without_data_loss(paths):
    # A database created by an older PRISM version: only the EvidenceStore
    # tables exist and it already holds a document row.
    store = EvidenceStore(paths)
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO documents (source_id, title, source, published_at,"
            " fetched_at, type, path, content, content_hash, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            LEGACY_ROW,
        )
    assert store.get("mat-legacy") is not None
    assert "graphiti_episode_registry" not in table_names(store.db_path)

    # First registry use on the OLD database adds the registry table in place.
    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    episode = make_episode()
    registry.put(
        episode, group_id="neo4j", graphiti_uuid="44444444-5555-6666-7777-888888888888"
    )
    assert registry.get(episode.episode_key) == episode
    registry.close()

    tables = table_names(store.db_path)
    assert {"documents", "document_fts", "graphiti_episode_registry"} <= tables
    # The legacy store and its row are untouched by the migration.
    assert store.get("mat-legacy") is not None
    store.close()

    # Reopening the store afterwards never drops the registry data.
    store = EvidenceStore(paths)
    store.initialize()
    assert store.get("mat-legacy") is not None
    reopened = SQLiteEpisodeRegistry(paths, database="neo4j")
    try:
        assert reopened.get(episode.episode_key) == episode
        assert (
            reopened.get_by_graphiti_uuid(
                "44444444-5555-6666-7777-888888888888", group_id="neo4j"
            )
            == episode
        )
    finally:
        reopened.close()
        store.close()


def test_registry_uses_the_evidence_store_database_file(tmp_path):
    paths = make_paths(tmp_path)
    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    store = EvidenceStore(paths)
    try:
        assert registry.db_path == store.db_path
        assert registry.db_path.parent == paths.resolve().data_dir
        assert registry.db_path.name == "index.db"
    finally:
        registry.close()
        store.close()


def test_close_is_idempotent_and_operations_after_close_fail_loudly(tmp_path):
    paths = make_paths(tmp_path)
    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    episode = make_episode()
    registry.put(episode, group_id="neo4j")

    registry.close()
    registry.close()  # idempotent
    assert registry.closed is True

    with pytest.raises(RuntimeError, match="closed"):
        registry.get(episode.episode_key)
    with pytest.raises(RuntimeError, match="closed"):
        registry.put(episode, group_id="neo4j")
    with pytest.raises(RuntimeError, match="closed"):
        registry.get_by_graphiti_uuid("anything", group_id="neo4j")


def test_context_manager_closes_the_registry(tmp_path):
    paths = make_paths(tmp_path)
    with SQLiteEpisodeRegistry(paths, database="neo4j") as registry:
        registry.put(make_episode(), group_id="neo4j")
    assert registry.closed is True


def test_registry_is_runtime_checkable_protocol_implementation(tmp_path):
    paths = make_paths(tmp_path)
    with SQLiteEpisodeRegistry(paths, database="neo4j") as registry:
        assert isinstance(registry, GraphEpisodeRegistry)
