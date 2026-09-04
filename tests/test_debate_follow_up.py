from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from prism.analyzer import EvolutionAnalysis, TimelineStage
from prism.debate import DebateLedger, DebateService
from prism.debate.models import FollowUpResult
from prism.domain import EvidenceLocator
from prism.llm import Completion

UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
CASE_ID = "case-follow-up"
PARENT_ID = "parent-run"


def run(coro):
    return asyncio.run(coro)


def make_analysis() -> EvolutionAnalysis:
    evidence = EvidenceLocator(
        source_id="source-1", corpus_path="corpus/a.md", paragraph=1,
        quote="The policy entered implementation.",
    )
    return EvolutionAnalysis(
        case_id=CASE_ID, as_of=AS_OF, case_type="policy",
        stages=(
            TimelineStage(
                episode_key="node-1", kind="evolution_node", layer="fact",
                summary="Implementation was recorded.", valid_at=AS_OF,
                invalid_at=None, reference_time=AS_OF, source_ids=("source-1",),
                node_type="implementation", evidence=(evidence,),
            ),
        ),
        turning_points=(), change_reasons=(), evidence_gaps=(), open_questions=(),
    )


class FakeAnalyzer:
    async def analyze(self, case_id, as_of=None, *, kinds=None):
        return make_analysis()


class FakeRouter:
    def __init__(self):
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        if '"phase":"independent"' in prompt:
            return Completion(json.dumps({
                "statements": [
                    {"id": "fact-1", "classification": "fact",
                     "text": "The policy entered implementation.",
                     "evidence_ids": ["node-1"]},
                    {"id": "why-1", "classification": "interpretation",
                     "text": "Implementation followed administrative preparation.",
                     "evidence_ids": ["node-1"]},
                ]
            }), provider="test", model="test")
        if '"phase":"cross_examination"' in prompt:
            return Completion(json.dumps({"challenges": []}), provider="test", model="test")
        return Completion(json.dumps({
            "consensus": [], "disagreements": [], "sources_of_disagreement": [],
            "key_evidence": [], "unresolved_questions": [],
            "falsification_conditions": [],
        }), provider="test", model="test")


def parent_service(tmp_path: Path, router: FakeRouter):
    ledger = DebateLedger(tmp_path / "index.db")
    return DebateService(FakeAnalyzer(), router, ledger=ledger), ledger


def test_follow_up_reuses_parent_snapshot_and_is_idempotent(tmp_path):
    router = FakeRouter()
    service, ledger = parent_service(tmp_path, router)
    parent = run(service.debate(CASE_ID, "Why?", AS_OF, perspectives=("institutional_regulatory",)))
    parent_id = ledger.entries(CASE_ID)[0].run_id
    calls_before = len(router.calls)

    result = run(service.follow_up(parent_id, "Why implementation?", "institutional_regulatory"))

    assert isinstance(result, FollowUpResult)
    assert result.parent_run_id == parent_id
    assert result.case_id == CASE_ID
    assert result.as_of == parent.as_of
    assert result.evidence_bundle_hash == parent.evidence_bundle_hash
    assert result.perspective_id == "institutional_regulatory"
    assert result.interpretation is not None
    assert result.interpretation.statements[0].evidence_ids == ("node-1",)
    assert len(router.calls) == calls_before + 1

    replay = run(service.follow_up(parent_id, "Why implementation?", "institutional_regulatory"))
    assert replay == result
    assert len(router.calls) == calls_before + 1

    reopened = DebateLedger(tmp_path / "index.db")
    saved = reopened.follow_up_entries(parent_id)
    assert len(saved) == 1
    assert saved[0].perspective_id == result.perspective_id
    assert saved[0].evidence_bundle_hash == parent.evidence_bundle_hash


def test_follow_up_rejects_unknown_parent_or_perspective_without_llm(tmp_path):
    router = FakeRouter()
    service, ledger = parent_service(tmp_path, router)
    run(service.debate(CASE_ID, "Why?", AS_OF, perspectives=("institutional_regulatory",)))
    parent_id = ledger.entries(CASE_ID)[0].run_id
    count = len(router.calls)

    with pytest.raises(LookupError):
        run(service.follow_up("missing", "Why?", "institutional_regulatory"))
    with pytest.raises(ValueError):
        run(service.follow_up(parent_id, "Why?", "not-a-profile"))
    with pytest.raises(ValueError):
        run(service.follow_up(parent_id, "", "institutional_regulatory"))
    assert len(router.calls) == count


def test_follow_up_rejects_changed_parent_evidence_bundle(tmp_path):
    router = FakeRouter()
    service, ledger = parent_service(tmp_path, router)
    run(service.debate(CASE_ID, "Why?", AS_OF, perspectives=("institutional_regulatory",)))
    parent_id = ledger.entries(CASE_ID)[0].run_id

    changed = EvolutionAnalysis(
        case_id=CASE_ID, as_of=AS_OF, case_type="policy",
        stages=(TimelineStage(
            episode_key="node-1", kind="evolution_node", layer="fact",
            summary="A changed summary.", valid_at=AS_OF, invalid_at=None,
            reference_time=AS_OF, source_ids=("source-1",), node_type="implementation",
            evidence=(EvidenceLocator(
                source_id="source-1", corpus_path="corpus/a.md", paragraph=1,
                quote="A changed quote.",
            ),),
        ),), turning_points=(), change_reasons=(), evidence_gaps=(), open_questions=(),
    )

    class ChangedAnalyzer:
        async def analyze(self, case_id, as_of=None, *, kinds=None):
            return changed

    changed_service = DebateService(ChangedAnalyzer(), router, ledger=ledger)
    with pytest.raises(ValueError, match="evidence bundle"):
        run(changed_service.follow_up(parent_id, "Why?", "institutional_regulatory"))


def _async(value):
    async def result():
        return value
    return result()


def test_follow_up_rejects_unknown_evidence_id_from_provider(tmp_path):
    router = FakeRouter()
    service, ledger = parent_service(tmp_path, router)
    run(service.debate(CASE_ID, "Why?", AS_OF, perspectives=("institutional_regulatory",)))
    parent_id = ledger.entries(CASE_ID)[0].run_id

    class BadRouter(FakeRouter):
        async def complete(self, role, prompt):
            self.calls.append((role, prompt))
            return Completion(json.dumps({"statements": [{
                "id": "bad", "classification": "fact", "text": "Unsupported.",
                "evidence_ids": ["not-in-parent-bundle"],
            }]}), provider="test", model="test")

    bad = BadRouter()
    result = run(DebateService(FakeAnalyzer(), bad, ledger=ledger).follow_up(
        parent_id, "Why?", "institutional_regulatory"
    ))
    assert result.status == "failed"
    assert result.interpretation is None
    assert result.errors[0].error_code == "invalid_output"


def test_cli_follow_up_delegates_to_facade():
    from io import StringIO
    from prism.cli.main import main

    @dataclass
    class Api:
        calls: list = None
        def __post_init__(self): self.calls = []
        async def follow_up_debate(self, parent_run_id, question, perspective):
            self.calls.append((parent_run_id, question, perspective))
            return {"parent_run_id": parent_run_id, "status": "completed"}

    api = Api()
    stdout, stderr = StringIO(), StringIO()
    status = run(main(
        ["follow-up", PARENT_ID, "--perspective", "institutional_regulatory",
         "--question", "Why?"], api=api, stdout=stdout, stderr=stderr,
    ))
    assert status == 0
    assert stderr.getvalue() == ""
    assert api.calls == [(PARENT_ID, "Why?", "institutional_regulatory")]
    assert '"parent_run_id":"parent-run"' in stdout.getvalue()
