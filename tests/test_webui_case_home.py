from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from prism.analyzer import HistoricalCaseState, TimelineStage
from prism.domain import EvidenceLocator
from prism.webui import CaseHomeController, WebUIUnavailableError, create_app, run as run_webui

UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class Overview:
    case_id: str
    case_type: str
    canonical_name: str
    status: str
    material_count: int
    earliest_observed: datetime | None
    latest_observed: datetime | None
    latest_node_at: datetime | None
    last_updated: datetime
    unresolved_gap_count: int = 0
    unresolved_conflict_count: int = 0


class FakeAPI:
    def __init__(self):
        self.overview_calls = []
        self.snapshot_calls = []
        self.items = (
            Overview("case-a", "policy", "Case A", "active", 2, AS_OF, AS_OF, AS_OF, AS_OF),
            Overview("case-b", "academic", "Case B", "paused", 1, AS_OF, AS_OF, None, AS_OF, 1),
        )

    async def case_overviews(self, **filters):
        self.overview_calls.append(filters)
        return self.items

    async def query_historical_snapshot(self, case_id, as_of, *, stage=None, kinds=None):
        self.snapshot_calls.append((case_id, as_of, stage, kinds))
        evidence = EvidenceLocator("source-a", "corpus/a.md", paragraph=2, quote="Recorded text.")
        stage_item = TimelineStage(
            episode_key="node-a", kind="evolution_node", layer="fact",
            summary="Published.", valid_at=AS_OF, invalid_at=None,
            reference_time=AS_OF, source_ids=("source-a",),
            node_type="publication", evidence=(evidence,),
        )
        return HistoricalCaseState(
            case_id=case_id, cutoff_at=as_of, case_type="policy", status="active",
            nodes=(stage_item,), facts=(), interpretations=(), relations=(),
            invalidated_facts=(), evidence_gaps=(),
        )


def test_case_home_loads_filtered_overviews_as_json_safe_view_model():
    api = FakeAPI()
    controller = CaseHomeController(api)

    payload = run(controller.list_cases(query="case-a", case_type="policy", unresolved_only=True))

    assert api.overview_calls == [{
        "case_id": "case-a", "case_type": "policy", "status": None,
        "unresolved_only": True, "order": "case_id", "reverse": False,
    }]
    assert payload[0]["case_id"] == "case-a"
    assert payload[0]["last_updated"] == AS_OF.isoformat()


def test_case_home_snapshot_delegates_to_shared_facade_and_preserves_evidence():
    api = FakeAPI()
    controller = CaseHomeController(api)

    payload = run(controller.snapshot("case-a", AS_OF, stage="publication", kinds=("evolution_node",)))

    assert api.snapshot_calls == [("case-a", AS_OF, "publication", ("evolution_node",))]
    assert payload["case_id"] == "case-a"
    assert payload["nodes"][0]["evidence"][0]["corpus_path"] == "corpus/a.md"
    assert payload["nodes"][0]["evidence"][0]["quote"] == "Recorded text."


def test_case_home_rejects_invalid_snapshot_inputs_before_api_call():
    api = FakeAPI()
    controller = CaseHomeController(api)

    with pytest.raises(ValueError, match="case_id"):
        run(controller.snapshot("", AS_OF))
    with pytest.raises(ValueError, match="timezone-aware"):
        run(controller.snapshot("case-a", datetime(2026, 9, 1)))
    with pytest.raises(ValueError, match="stage"):
        run(controller.snapshot("case-a", AS_OF, stage="invented"))
    assert api.snapshot_calls == []


def test_webui_import_and_missing_optional_dependency_are_safe(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr("prism.webui.app._nicegui", lambda: (_ for _ in ()).throw(ImportError()))

    with pytest.raises(WebUIUnavailableError, match="webui"):
        create_app(api)


def test_webui_run_refuses_non_loopback_before_optional_import():
    with pytest.raises(ValueError, match="loopback"):
        run_webui(FakeAPI(), host="0.0.0.0")
