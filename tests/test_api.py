"""Focused contract tests for the CLI/WebUI API facade (module 7)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import pytest

from prism.api import PrismAPI
from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.events import Event
from prism.graph import GraphTimeline, GraphWriteResult
from prism.ingestion import IngestionResult
from prism.store import IndexEntry, IndexOutcome


NOW = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def material() -> Material:
    return Material(
        id="material-1",
        title="Policy notice",
        source="example.gov",
        published_at=NOW,
        fetched_at=NOW,
        type="policy",
        content="Policy evidence.",
        original_format="md",
    )


def ingestion_result(tmp_path: Path) -> IngestionResult:
    source = tmp_path / "source.md"
    corpus = tmp_path / "corpus" / "source.md"
    return IngestionResult(material(), source, corpus, False, "direct")


def index_outcome() -> IndexOutcome:
    item = material()
    return IndexOutcome(
        IndexEntry(
            source_id=item.id,
            title=item.title,
            source=item.source,
            published_at=item.published_at,
            fetched_at=item.fetched_at,
            type=item.type,
            content=item.content,
            path="corpus/source.md",
            content_hash="digest",
        ),
        "indexed",
    )


class FakeIngestionService:
    def __init__(self, result: IngestionResult):
        self.result = result
        self.calls: list[tuple[object, object]] = []

    def ingest(self, path, metadata=None):
        self.calls.append((path, metadata))
        return self.result


class FakeEvidenceStore:
    def __init__(self, outcome: IndexOutcome):
        self.outcome = outcome
        self.calls: list[object] = []
        self.search_calls: list[object] = []
        self.search_result: list[object] = []

    def index_file(self, path):
        self.calls.append(path)
        return self.outcome

    def search(self, criteria, *, limit=50, offset=0):
        self.search_calls.append((criteria, limit, offset))
        return self.search_result


class FakeGraphService:
    def __init__(self):
        self.timeline_result = GraphTimeline("case-1", NOW, ())
        self.write_result = GraphWriteResult((), (), ())
        self.timeline_calls: list[tuple[str, datetime]] = []
        self.add_case_calls: list[tuple[object, dict[str, object]]] = []

    async def timeline(self, case_id, as_of):
        self.timeline_calls.append((case_id, as_of))
        return self.timeline_result

    async def add_case(self, case, **bundle):
        self.add_case_calls.append((case, bundle))
        return self.write_result


class FakeEventBus:
    def __init__(self):
        self.events: list[Event] = []

    async def publish(self, event):
        self.events.append(event)


def facade(tmp_path: Path):
    ingestion = FakeIngestionService(ingestion_result(tmp_path))
    store = FakeEvidenceStore(index_outcome())
    graph = FakeGraphService()
    bus = FakeEventBus()
    return PrismAPI(ingestion, store, graph, bus), ingestion, store, graph, bus


@pytest.mark.parametrize(
    ("missing_index", "message"),
    [
        (0, "ingestion_service"),
        (1, "evidence_store"),
        (2, "graph_service"),
        (3, "event_bus"),
    ],
)
def test_facade_rejects_missing_required_dependencies(
    tmp_path, missing_index, message
):
    dependencies = [
        FakeIngestionService(ingestion_result(tmp_path)),
        FakeEvidenceStore(index_outcome()),
        FakeGraphService(),
        FakeEventBus(),
    ]
    dependencies[missing_index] = None

    with pytest.raises(ValueError, match=message):
        PrismAPI(*dependencies)


def test_search_delegates_to_store_with_explicit_filters(tmp_path):
    api, _, store, _, _ = facade(tmp_path)
    expected = ["hit"]
    store.search_result = expected

    result = run(
        api.search(
            "housing",
            case_tag="case-1",
            source="example.gov",
            type="policy",
            published_after=NOW,
            published_before=NOW,
            limit=7,
            offset=2,
        )
    )

    assert result is expected
    criteria, limit, offset = store.search_calls[0]
    assert criteria.query == "housing"
    assert criteria.case_tag == "case-1"
    assert criteria.source == "example.gov"
    assert criteria.type == "policy"
    assert criteria.published_after == NOW
    assert criteria.published_before == NOW
    assert (limit, offset) == (7, 2)


def test_ingest_material_normalizes_indexes_publishes_and_preserves_result(tmp_path):
    api, ingestion, store, _, bus = facade(tmp_path)
    metadata = {"source": "example.gov", "case_tags": ["case-1"]}

    result = run(api.ingest_material(tmp_path / "input.md", metadata))

    assert result is ingestion.result
    assert ingestion.calls == [(tmp_path / "input.md", metadata)]
    assert ingestion.calls[0][1] is metadata
    assert store.calls == [ingestion.result.corpus_path]
    assert len(bus.events) == 1
    event = bus.events[0]
    assert event.event_type == "material.ingested"
    assert event.payload["material_id"] == ingestion.result.material.id
    assert event.payload["corpus_path"] == str(ingestion.result.corpus_path)
    assert event.payload["index_status"] == "indexed"
    assert event.correlation_id == ingestion.result.material.id
    assert event.occurred_at.tzinfo is not None


def test_ingest_material_awaits_publish_instead_of_leaking_a_background_task(
    tmp_path,
):
    class BlockingBus(FakeEventBus):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def publish(self, event):
            self.started.set()
            await self.release.wait()
            await super().publish(event)

    async def scenario():
        ingestion = FakeIngestionService(ingestion_result(tmp_path))
        store = FakeEvidenceStore(index_outcome())
        bus = BlockingBus()
        api = PrismAPI(ingestion, store, FakeGraphService(), bus)

        call = asyncio.create_task(api.ingest_material("input.md", {}))
        await asyncio.wait_for(bus.started.wait(), timeout=1)
        assert not call.done()

        bus.release.set()
        assert await asyncio.wait_for(call, timeout=1) is ingestion.result
        await asyncio.sleep(0)
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    run(scenario())


@pytest.mark.parametrize("failure_stage", ["ingestion", "index", "publish"])
def test_ingest_material_propagates_stage_errors(tmp_path, failure_stage):
    expected = RuntimeError(f"{failure_stage} failed")
    result = ingestion_result(tmp_path)

    class Ingestion(FakeIngestionService):
        def ingest(self, path, metadata=None):
            if failure_stage == "ingestion":
                raise expected
            return super().ingest(path, metadata)

    class Store(FakeEvidenceStore):
        def index_file(self, path):
            if failure_stage == "index":
                raise expected
            return super().index_file(path)

    class Bus(FakeEventBus):
        async def publish(self, event):
            if failure_stage == "publish":
                raise expected
            await super().publish(event)

    ingestion = Ingestion(result)
    store = Store(index_outcome())
    bus = Bus()
    api = PrismAPI(ingestion, store, FakeGraphService(), bus)

    with pytest.raises(RuntimeError) as raised:
        run(api.ingest_material("input.md", {}))

    assert raised.value is expected
    if failure_stage == "ingestion":
        assert store.calls == []
    if failure_stage in {"ingestion", "index"}:
        assert bus.events == []


def test_timeline_entry_points_delegate_and_preserve_graph_result(tmp_path):
    api, _, _, graph, _ = facade(tmp_path)

    built = run(api.build_timeline("case-1", NOW))
    queried = run(api.query_history("case-1", NOW))

    assert built is graph.timeline_result
    assert queried is graph.timeline_result
    assert graph.timeline_calls == [("case-1", NOW), ("case-1", NOW)]


def test_add_case_bundle_delegates_domain_objects_and_preserves_result(tmp_path):
    api, _, _, graph, _ = facade(tmp_path)
    case = EvolutionCase("case-1", "policy", "Policy", NOW, "active")
    node = EvolutionNode("node-1", "case-1", "publication", NOW, "Published")
    fact = TemporalFact(
        "Agency", "published", "Policy", NOW, None, NOW, ("material-1",), 1.0, "source"
    )
    claim = Claim("claim-1", "Agency", "Policy applies", "support", NOW)
    source = material()

    result = run(
        api.add_case_bundle(
            case,
            nodes=[node],
            facts=[fact],
            claims=[claim],
            materials=[source],
        )
    )

    assert result is graph.write_result
    assert graph.add_case_calls == [
        (
            case,
            {
                "nodes": [node],
                "facts": [fact],
                "claims": [claim],
                "materials": [source],
            },
        )
    ]


def test_facade_does_not_wrap_graph_errors(tmp_path):
    api, _, _, graph, _ = facade(tmp_path)
    expected = LookupError("case missing")

    async def fail(case_id, as_of):
        raise expected

    graph.timeline = fail

    with pytest.raises(LookupError) as raised:
        run(api.build_timeline("missing", NOW))

    assert raised.value is expected


class _Audit:
    def __init__(self, material_id: str, decision: str):
        self.material_id = material_id
        self.decision = decision


class FakeAdjudicator:
    def __init__(self, records=()):
        self.records = tuple(records)
        self.calls: list[str | None] = []

    def history(self, material_id=None):
        self.calls.append(material_id)
        if material_id is None:
            return self.records
        return tuple(record for record in self.records if record.material_id == material_id)


def _api_with_adjudicator(tmp_path: Path, adjudicator: object | None) -> PrismAPI:
    return PrismAPI(
        FakeIngestionService(ingestion_result(tmp_path)),
        FakeEvidenceStore(index_outcome()),
        FakeGraphService(),
        FakeEventBus(),
        adjudicator=adjudicator,
    )


def test_adjudication_history_delegates_and_preserves_records(tmp_path):
    records = (_Audit("mat-1", "rejected"), _Audit("mat-2", "accepted"))
    adjudicator = FakeAdjudicator(records)
    api = _api_with_adjudicator(tmp_path, adjudicator)

    all_records = api.adjudication_history()
    assert all_records == records
    assert adjudicator.calls == [None]

    scoped = api.adjudication_history("mat-1")
    assert scoped == (records[0],)
    assert adjudicator.calls == [None, "mat-1"]


def test_adjudication_history_is_empty_without_an_adjudicator(tmp_path):
    api = facade(tmp_path)[0]
    assert api.adjudication_history() == ()
    assert api.adjudication_history("mat-1") == ()


def test_adjudication_history_surfaces_batch_failure_records(tmp_path):
    """The audit API shows candidate_kind='batch' adjudication failures."""
    from prism.adjudication import (
        AdjudicationBatchFailure,
        AdjudicationLedger,
        AdjudicationService,
    )
    from prism.domain import EvidenceLocator, EvolutionNode
    from prism.extraction import ExtractionResult

    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    material = Material(
        id="mat-1",
        title="t",
        source="s",
        published_at=now,
        fetched_at=now,
        type="news",
        content="A quote",
    )
    evidence = EvidenceLocator("mat-1", "corpus/a.md", paragraph=1, quote="A quote")
    node = EvolutionNode(
        "n1", "case-x", "publication", now, "A quote", ("mat-1",),
        evidence=(evidence,), valid_at=now, observed_at=now,
        provenance_type="source_explicit", evidence_role="primary_observation",
    )
    extraction = ExtractionResult(nodes=(node,))

    class _BadRouter:
        async def complete(self, role, prompt):
            return type("C", (), {"text": '{"decisions":[{"candidate_kind":"node",'
                                          '"candidate_id":"n1","decision":"accepted",'
                                          '"reason":"ok","extra":1}]}'})()

    db_path = tmp_path / "adjudication.db"
    service = AdjudicationService(_BadRouter(), ledger=AdjudicationLedger(db_path))
    with pytest.raises(AdjudicationBatchFailure):
        run(service.adjudicate(material, extraction))

    api = _api_with_adjudicator(tmp_path, service)
    records = api.adjudication_history("mat-1")
    assert len(records) == 1
    assert records[0].candidate_kind == "batch"
    assert records[0].decision == "adjudication_failed"
    assert records[0].revalidation_outcome == "adjudication_failed"
    # The history view is durable: a fresh ledger read sees the same row.
    assert len(AdjudicationLedger(db_path).entries("mat-1")) == 1


def test_adjudication_history_batch_failure_decision_is_a_formal_enum(
    tmp_path,
):
    """Batch-failure records served by the audit API after a ledger restart
    keep decision as the AdjudicationDecision enum member ADJUDICATION_FAILED
    (with .value), never a bare string that breaks decision.value callers."""
    from prism.adjudication import (
        AdjudicationBatchFailure,
        AdjudicationDecision,
        AdjudicationLedger,
        AdjudicationService,
    )
    from prism.domain import EvidenceLocator
    from prism.extraction import ExtractionResult

    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    material = Material(
        id="mat-1",
        title="t",
        source="s",
        published_at=now,
        fetched_at=now,
        type="news",
        content="A quote",
    )
    evidence = EvidenceLocator("mat-1", "corpus/a.md", paragraph=1, quote="A quote")
    node = EvolutionNode(
        "n1", "case-x", "publication", now, "A quote", ("mat-1",),
        evidence=(evidence,), valid_at=now, observed_at=now,
        provenance_type="source_explicit", evidence_role="primary_observation",
    )
    extraction = ExtractionResult(nodes=(node,))

    class _BadRouter:
        async def complete(self, role, prompt):
            return type("C", (), {"text": '{"decisions":[{"candidate_kind":"node",'
                                          '"candidate_id":"n1","decision":"accepted",'
                                          '"reason":"ok","extra":1}]}'})()

    db_path = tmp_path / "adjudication.db"
    first = AdjudicationService(_BadRouter(), ledger=AdjudicationLedger(db_path))
    with pytest.raises(AdjudicationBatchFailure):
        run(first.adjudicate(material, extraction))

    # Restart: a fresh service and ledger over the same durable SQLite file.
    restarted = AdjudicationService(_BadRouter(), ledger=AdjudicationLedger(db_path))
    api = _api_with_adjudicator(tmp_path, restarted)
    records = api.adjudication_history("mat-1")
    assert len(records) == 1
    batch = records[0]
    assert batch.candidate_kind == "batch"
    assert isinstance(batch.decision, AdjudicationDecision)
    assert batch.decision is AdjudicationDecision.ADJUDICATION_FAILED
    assert batch.decision.value == "adjudication_failed"
    assert batch.revalidation_outcome == "adjudication_failed"
    # Consumers may safely use decision.value on every API-surfaced record.
    assert {record.decision.value for record in api.adjudication_history()} == {
        "adjudication_failed"
    }


def test_adjudication_history_tolerates_an_adjudicator_without_history(tmp_path):
    api = _api_with_adjudicator(tmp_path, object())
    assert api.adjudication_history("mat-1") == ()
