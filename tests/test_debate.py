"""Offline M2 tests for automatic multi-perspective debate."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from prism.analyzer import EvolutionAnalysis, TimelineStage
from prism.domain import EvidenceLocator
from prism.llm import Completion
from prism.report import ReportService

UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
EARLY = datetime(2026, 8, 15, tzinfo=UTC)
CASE_ID = "case-policy"
QUESTION = "Why did the policy interpretation change?"
SECRET = "sk-test-secret-value"
ABSOLUTE_PATH = "E:\\Private\\material.md"


def run(coro):
    return asyncio.run(coro)


def stage(
    key: str,
    kind: str = "evolution_node",
    *,
    layer: str = "fact",
    relation_type: str | None = None,
    source_ids: tuple[str, ...] = ("source-1",),
    valid_at: datetime = AS_OF,
    quote: str = "The agency narrowed the scope.",
) -> TimelineStage:
    return TimelineStage(
        episode_key=key,
        kind=kind,
        layer=layer,
        summary=f"Recorded {key}",
        valid_at=valid_at,
        invalid_at=None,
        reference_time=valid_at,
        source_ids=source_ids,
        node_type="publication" if kind == "evolution_node" else None,
        relation_type=relation_type,
        evidence=(
            EvidenceLocator(
                source_id=source_ids[0],
                corpus_path="corpus/policy.md",
                paragraph=1,
                quote=quote,
            ),
        )
        if source_ids
        else (),
    )


def analysis(case_type: str = "policy") -> EvolutionAnalysis:
    return EvolutionAnalysis(
        case_id=CASE_ID,
        as_of=AS_OF,
        case_type=case_type,
        stages=(
            stage("case-1", "evolution_case", source_ids=()),
            stage("node-1"),
            stage("fact-1", "temporal_fact"),
            stage("claim-1", "claim", layer="interpretation"),
            stage("conflict-1", "temporal_relation", relation_type="contradicts"),
        ),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
        invalidated_stages=(stage("old-fact-1", "temporal_fact"),),
    )


class FakeAnalyzer:
    def __init__(self, result: EvolutionAnalysis | None = None):
        self.result = result or analysis()
        self.calls: list[tuple[str, datetime | None]] = []

    async def analyze(self, case_id, as_of=None, *, kinds=None):
        self.calls.append((case_id, as_of))
        return self.result


def independent(profile: str, *, evidence_id: str = "node-1") -> dict:
    prefix = profile.split("_", 1)[0]
    return {
        "statements": [
            {
                "id": f"{prefix}-fact",
                "classification": "fact",
                "text": f"{profile} records the publication.",
                "evidence_ids": [evidence_id],
            },
            {
                "id": f"{prefix}-interpretation",
                "classification": "interpretation",
                "text": f"{profile} explains the change as scope narrowing.",
                "evidence_ids": [evidence_id],
            },
            {
                "id": f"{prefix}-value",
                "classification": "value_judgment",
                "text": f"{profile} values procedural clarity.",
                "evidence_ids": [evidence_id],
            },
            {
                "id": f"{prefix}-prediction",
                "classification": "prediction",
                "text": f"{prefix} expects slower implementation.",
                "evidence_ids": [evidence_id],
            },
            {
                "id": f"{prefix}-unresolved",
                "classification": "unresolved",
                "text": f"{profile} cannot determine enforcement effects.",
                "evidence_ids": [],
            },
        ]
    }


def cross(profile: str) -> dict:
    return {
        "challenges": [
            {
                "challenge_id": f"{profile}-challenge",
                "target_profile_id": "industry_execution",
                "target_statement_id": "industry-fact",
                "challenge": "The implementation claim needs a recorded source.",
                "reply": "The publication record remains supported.",
                "withdrawn": False,
                "evidence_ids": ["node-1"],
            }
        ]
    }


def synthesis_payload() -> dict:
    return {
        "consensus": [
            {
                "text": "The recorded evidence says the scope narrowed.",
                "evidence_ids": ["node-1"],
            }
        ],
        "disagreements": [
            {
                "text": "The perspectives differ on implementation speed.",
                "evidence_ids": ["node-1"],
            }
        ],
        "sources_of_disagreement": [
            {
                "text": "The disagreement is about expected execution capacity.",
                "evidence_ids": ["node-1"],
            }
        ],
        "key_evidence": [
            {"evidence_id": "node-1", "rationale": "The publication directly records the change."}
        ],
        "unresolved_questions": [
            {
                "text": "No recorded evidence measures the enforcement effect.",
                "evidence_ids": ["node-1"],
            }
        ],
        "falsification_conditions": [
            {
                "text": "A later implementation record contradicting slower execution would falsify the prediction.",
                "evidence_ids": ["node-1"],
            }
        ],
    }


def phase(prompt: str) -> str:
    return json.loads(prompt.split("BEGIN REQUEST\n", 1)[1].split("\nEND REQUEST", 1)[0])["phase"]


def perspective(prompt: str) -> str:
    return json.loads(prompt.split("BEGIN REQUEST\n", 1)[1].split("\nEND REQUEST", 1)[0]).get(
        "perspective_id"
    )


class ScriptedRouter:
    def __init__(self, independent=None, cross=None, synthesis=None, fail_profiles=(), fail_all=False):
        self.independent = independent
        self.cross = cross
        self.synthesis = synthesis
        self.fail_profiles = fail_profiles
        self.fail_all = fail_all
        self.calls = []
        self.prompts = []

    async def complete(self, role, prompt):
        self.calls.append((role, phase(prompt)))
        if self.fail_all:
            raise RuntimeError("offline transport failure")
        current = perspective(prompt)
        if phase(prompt) == "independent":
            if current in self.fail_profiles:
                raise RuntimeError("one perspective failed")
            payload = self.independent or independent(current)
            text = payload if isinstance(payload, str) else json.dumps(payload)
        elif phase(prompt) == "cross_examination":
            if current in self.fail_profiles:
                raise RuntimeError("one perspective failed")
            payload = self.cross or cross(current)
            text = payload if isinstance(payload, str) else json.dumps(payload)
        else:
            payload = self.synthesis or synthesis_payload()
            text = payload if isinstance(payload, str) else json.dumps(payload)
        return Completion(text=text, provider="offline", model="test")


def service(tmp_path: Path, router=None, analyzer=None):
    from prism.debate import DebateLedger, DebateService

    return DebateService(
        analyzer or FakeAnalyzer(),
        router,
        ledger=DebateLedger(tmp_path / "index.db"),
        profiles=None,
    )


def test_profiles_and_academic_case_type_filtering():
    from prism.debate import ACADEMIC_PROFILES, DEFAULT_PROFILES

    assert [profile.id for profile in DEFAULT_PROFILES] == [
        "institutional_regulatory",
        "industry_execution",
        "affected_groups",
        "academic_observer",
    ]
    assert [profile.id for profile in ACADEMIC_PROFILES] == [
        "experimental_methods",
        "mechanism_explanation",
        "evidence_quality",
        "research_history",
    ]
    assert all(not profile.preset_conclusion for profile in DEFAULT_PROFILES)
    assert all(not profile.preset_conclusion for profile in ACADEMIC_PROFILES)


def test_debate_shares_one_cutoff_bundle_and_distinguishes_statement_types(tmp_path):
    router = ScriptedRouter()
    fake = FakeAnalyzer()
    result = run(service(tmp_path, router, fake).debate(CASE_ID, QUESTION, AS_OF))

    assert fake.calls == [(CASE_ID, AS_OF)]
    assert result.case_id == CASE_ID
    assert result.question == QUESTION
    assert result.as_of == AS_OF
    assert result.status == "completed"
    assert result.replayed is False
    assert [item.profile_id for item in result.results] == [
        "institutional_regulatory",
        "industry_execution",
        "affected_groups",
        "academic_observer",
    ]

    first = result.results[0]
    classifications = [statement.classification for statement in first.interpretation.statements]
    assert classifications == [
        "fact",
        "interpretation",
        "value_judgment",
        "prediction",
        "unresolved",
    ]
    assert first.interpretation.statements[0].evidence_ids == ("node-1",)
    assert first.cross_examination is not None
    assert first.cross_examination.challenges[0].withdrawn is False

    assert result.synthesis is not None
    assert result.synthesis.consensus[0].text == "The recorded evidence says the scope narrowed."
    assert result.synthesis.key_evidence[0].evidence_id == "node-1"
    assert result.synthesis.falsification_conditions[0].evidence_ids == ("node-1",)
    assert result.fallback_reason is None
    assert result.evidence_bundle_hash
    assert result.automatic_adjudication is True


def test_all_perspectives_receive_identical_readonly_evidence_and_cutoff_is_applied(tmp_path):
    router = ScriptedRouter()
    fake = FakeAnalyzer()
    run(service(tmp_path, router, fake).debate(CASE_ID, QUESTION, EARLY))

    assert fake.calls == [(CASE_ID, EARLY)]
    assert len(router.calls) == 9
    assert [phase_name for _, phase_name in router.calls] == [
        "independent",
        "independent",
        "independent",
        "independent",
        "cross_examination",
        "cross_examination",
        "cross_examination",
        "cross_examination",
        "synthesis",
    ]
    # The fake analyzer's single result is reused verbatim; no per-profile
    # graph access happens. Every result derives from that one bundle object.
    assert fake.calls == [(CASE_ID, EARLY)]


def test_cross_examination_receives_all_independent_outputs(tmp_path):
    router = ScriptedRouter()
    fake = FakeAnalyzer()

    class PromptRouter(ScriptedRouter):
        def __init__(self):
            super().__init__()
            self.prompts = []

        async def complete(self, role, prompt):
            self.prompts.append((role, prompt))
            return await super().complete(role, prompt)

    router = PromptRouter()
    run(service(tmp_path, router, fake).debate(CASE_ID, QUESTION, AS_OF))
    cross_prompt = router.prompts[4][1]
    for profile in (
        "institutional_regulatory",
        "industry_execution",
        "affected_groups",
        "academic_observer",
    ):
        assert f"{profile.split('_', 1)[0]}-fact" in cross_prompt
    assert "BEGIN INDEPENDENT OUTPUTS" in cross_prompt
    assert "BEGIN EVIDENCE BUNDLE" in cross_prompt


def test_unknown_citation_and_strict_json_are_safely_degraded(tmp_path):
    raw_cases = [
        '{"statements":[{"id":"a-fact","classification":"fact","text":"ok","evidence_ids":["ghost"]}], "extra":1}',
        '{"statements":[{"id":"a-fact","classification":"fact","text":"ok","evidence_ids":["ghost"]}]}',
        '{"statements":[{"id":"a-fact","classification":"fact","text":"ok","evidence_ids":[NaN]}]}',
        '{"statements":[{"id":"a-fact","classification":"fact","text":"ok","evidence_ids":["node-1"]}],"statements":[]}',
        '{"statements":[{"id":"a-fact","classification":"not-a-class","text":"ok","evidence_ids":["node-1"]}]}',
        '{"statements":[{"id":"a-fact","classification":"fact","text":"ok","evidence_ids":"node-1"}]}',
        '{"statements":[{"id":"a-fact","classification":"fact","evidence_ids":["node-1"]}]}',
    ]
    for text in raw_cases:
        router = ScriptedRouter(independent=text)
        result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))
        assert result.status in {"degraded", "no_conclusion"}
        assert all(item.status == "unavailable" for item in result.results)
        assert result.errors


def test_single_perspective_failure_is_isolated(tmp_path):
    router = ScriptedRouter(fail_profiles=("institutional_regulatory",))
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

    assert [item.status for item in result.results] == [
        "unavailable",
        "available",
        "available",
        "available",
    ]
    failed = result.results[0]
    assert failed.failure is not None
    assert failed.failure.error_code == "llm_failure"
    assert result.status == "completed_with_unavailable_perspectives"
    assert result.synthesis is not None
    assert all(item.cross_examination is not None for item in result.results[1:])


def test_all_llm_failures_return_conservative_no_conclusion_result(tmp_path):
    router = ScriptedRouter(fail_all=True)
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

    assert result.status == "no_conclusion"
    assert result.synthesis is None
    assert all(item.status == "unavailable" for item in result.results)
    assert len(result.errors) == 4
    assert "no debate conclusion" in result.fallback_reason.lower()
    assert len(router.calls) == 4


def test_no_router_or_missing_role_is_an_audited_degradation(tmp_path):
    result = run(service(tmp_path, None).debate(CASE_ID, QUESTION, AS_OF))

    assert result.status == "no_conclusion"
    assert result.synthesis is None
    assert result.fallback_reason == "debate LLM role is unavailable"
    assert result.automatic_adjudication is True
    assert result.errors


def test_router_without_debate_role_degrades_without_fabricating_conclusions(tmp_path):
    from prism.llm import LLMRouter, Provider, TaskRole, TaskRoute

    class UnusedTransport:
        async def complete(self, **kwargs):
            raise AssertionError("the extract-only route must not be called")

        async def test_connection(self, **kwargs):
            return True

    router = LLMRouter(
        providers=(
            Provider(
                name="offline",
                base_url="https://llm.example.test/v1",
                api_key_env="OFFLINE_LLM_KEY",
                default_model="test",
            ),
        ),
        routes=(TaskRoute(TaskRole.EXTRACT, ("offline",)),),
        transport=UnusedTransport(),
    )
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

    assert result.status == "no_conclusion"
    assert result.synthesis is None
    assert all(item.failure.error_code == "llm_unavailable" for item in result.results)
    assert "no debate conclusion" in result.fallback_reason


def test_synthesis_failure_uses_deterministic_conservative_fallback(tmp_path):
    router = ScriptedRouter(synthesis="not json")
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

    assert result.status == "degraded"
    assert result.synthesis is not None
    assert result.fallback_reason == "synthesis invalid; deterministic conservative summary used"
    assert result.synthesis.consensus == ()


def test_prompts_are_secret_free_and_absolute_path_free(tmp_path):
    router = ScriptedRouter()
    result = run(service(tmp_path, router).debate(CASE_ID, f"{QUESTION} {SECRET} {ABSOLUTE_PATH}", AS_OF))

    for prompt in router.prompts:
        assert SECRET not in prompt
        assert ABSOLUTE_PATH not in prompt
    # The service sanitizes the user question and only passes structured
    # evidence; the test router saw no secret or absolute path in any call.
    assert result.question == QUESTION
    assert result.evidence_bundle_hash


def test_ledger_restarts_and_replays_the_same_input_without_new_llm_calls(tmp_path):
    db = tmp_path / "index.db"
    router = ScriptedRouter()
    fake = FakeAnalyzer()
    debate = service(db.parent, router, fake)
    first = run(debate.debate(CASE_ID, QUESTION, AS_OF))
    assert first.replayed is False

    from prism.debate import DebateLedger

    restarted_router = ScriptedRouter()
    restarted = type(debate)(
        fake,
        restarted_router,
        ledger=DebateLedger(db),
        profiles=None,
    )
    second = run(restarted.debate(CASE_ID, QUESTION, AS_OF))
    assert second.replayed is True
    assert second == replace(first, replayed=True)
    assert restarted_router.calls == []

    history = DebateLedger(db).entries(CASE_ID)
    assert len(history) == 1
    assert history[0].case_id == CASE_ID
    assert history[0].question == QUESTION
    assert history[0].evidence_bundle_hash == first.evidence_bundle_hash
    assert json.loads(history[0].rounds_json)


def test_runtime_and_api_expose_debate_case_with_cli_consistency(tmp_path, monkeypatch):
    from prism.api import PrismAPI
    from prism.cli import main
    from prism.debate import DebateService
    from prism.events import Event
    from prism.graph import GraphTimeline, GraphWriteResult
    from prism.ingestion import IngestionResult
    from prism.domain import Material
    from prism.store import IndexEntry, IndexOutcome

    monkeypatch.setenv("PRISM_HOME", str(tmp_path))
    material = Material(
        id="m-1", title="t", source="s", published_at=AS_OF,
        fetched_at=AS_OF, type="policy", content="content",
    )
    outcome = IndexOutcome(
        IndexEntry(
            source_id="m-1", title="t", source="s", published_at=AS_OF,
            fetched_at=AS_OF, type="policy", content="content",
            path="corpus/m.md", content_hash="h",
        ), "indexed",
    )

    class Ingestion:
        def ingest(self, path, metadata=None):
            return IngestionResult(material, Path("raw.md"), Path("corpus/m.md"), False, "direct")

    class Store:
        def index_file(self, path): return outcome
        def search(self, criteria, *, limit, offset): return []

    class Graph:
        async def timeline(self, case_id, as_of):
            return GraphTimeline(case_id, as_of, ())
        async def add_case(self, case, **bundle):
            return GraphWriteResult((), (), ())

    class Bus:
        async def publish(self, event: Event): pass

    debate = DebateService(FakeAnalyzer(), None)
    api = PrismAPI(
        Ingestion(), Store(), Graph(), Bus(), debate_service=debate
    )
    result = run(api.debate_case(CASE_ID, QUESTION, AS_OF, perspectives=("academic_observer",)))
    assert result.status == "no_conclusion"
    assert [item.profile_id for item in result.results] == ["academic_observer"]

    class CLIAPI:
        async def debate_case(self, case_id, question, as_of=None, perspectives=None):
            return result

    import io
    stdout = io.StringIO()
    stderr = io.StringIO()

    status = asyncio.run(
        main(
            [
                "debate",
                CASE_ID,
                "--question",
                QUESTION,
                "--as-of",
                AS_OF.isoformat(),
                "--perspectives",
                "academic_observer",
            ],
            api=CLIAPI(),
            stdout=stdout,
            stderr=stderr,
        )
    )
    assert status == 0
    rendered = json.loads(stdout.getvalue())
    assert rendered["case_id"] == result.case_id
    assert rendered["status"] == result.status
    assert rendered["profiles"] == ["academic_observer"]
    assert rendered["automatic_adjudication"] is True
    assert stderr.getvalue() == ""


def test_default_runtime_composes_debate_and_returns_audited_degradation(tmp_path, monkeypatch):
    from prism.runtime import create_runtime

    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "runtime-home"))

    async def flow():
        runtime = await create_runtime()
        try:
            result = await runtime.api.debate_case(CASE_ID, QUESTION, AS_OF)
        finally:
            await runtime.close()
        return result

    result = run(flow())

    assert result.status == "no_conclusion"
    assert result.automatic_adjudication is True
    assert result.fallback_reason == "debate LLM role is unavailable"
    assert len(result.results) == 4
    assert result.evidence_bundle_hash


def test_report_renders_debate_as_interpretation_separate_from_structured_facts(tmp_path):
    router = ScriptedRouter()
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))
    doc = run(ReportService().report(analysis(), debate_result=result))

    assert "## Debate Interpretation" in doc.markdown
    assert "not structured facts" in doc.markdown
    assert "The recorded evidence says the scope narrowed." in doc.markdown
    structured_body = doc.markdown.split("## Timeline Stages", 1)[1]
    assert "Debate Interpretation" not in structured_body
    assert doc.debate is result


def test_debate_is_automatic_and_never_requires_user_arbitration(tmp_path):
    router = ScriptedRouter()
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))
    assert result.automatic_adjudication is True
    assert "pending_user" not in result.status
    assert all(item.status != "awaiting_user" for item in result.results)


def test_cli_debate_matches_documented_contract(tmp_path):
    """FR-5 CLI: prism debate CASE_ID --question TEXT [--perspectives a,b]."""

    import io

    from prism.cli import main

    payload = run(service(tmp_path, ScriptedRouter()).debate(
        CASE_ID, QUESTION, AS_OF, perspectives=("academic_observer", "institutional_regulatory")
    ))

    class CLIAPI:
        async def debate_case(self, case_id, question, as_of=None, perspectives=None):
            return payload

    stdout = io.StringIO()
    stderr = io.StringIO()
    status = asyncio.run(
        main(
            [
                "debate",
                CASE_ID,
                "--question",
                QUESTION,
                "--perspectives",
                "academic_observer , institutional_regulatory",
            ],
            api=CLIAPI(),
            stdout=stdout,
            stderr=stderr,
        )
    )
    assert status == 0
    rendered = json.loads(stdout.getvalue())
    assert rendered["profiles"] == ["academic_observer", "institutional_regulatory"]
    assert rendered["automatic_adjudication"] is True
    assert stderr.getvalue() == ""

    legacy_stdout = io.StringIO()
    legacy_stderr = io.StringIO()
    legacy = asyncio.run(
        main(
            ["debate", CASE_ID, QUESTION],
            api=CLIAPI(),
            stdout=legacy_stdout,
            stderr=legacy_stderr,
        )
    )
    assert legacy == 2
    assert legacy_stdout.getvalue() == ""

    duplicate_stderr = io.StringIO()
    duplicate = asyncio.run(
        main(
            [
                "debate",
                CASE_ID,
                "--question",
                QUESTION,
                "--perspectives",
                "academic_observer,academic_observer",
            ],
            api=CLIAPI(),
            stdout=io.StringIO(),
            stderr=duplicate_stderr,
        )
    )
    assert duplicate == 2


def test_evidence_bundle_segments_are_identical_across_every_prompt(tmp_path):
    class PromptRouter(ScriptedRouter):
        def __init__(self):
            super().__init__()
            self.prompts = []

        async def complete(self, role, prompt):
            self.prompts.append(prompt)
            return await super().complete(role, prompt)

    router = PromptRouter()
    run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

    segments = [
        prompt.split("BEGIN EVIDENCE BUNDLE\n", 1)[1].split("\nEND EVIDENCE BUNDLE", 1)[0]
        for prompt in router.prompts
    ]
    assert len(segments) == 9
    assert len(set(segments)) == 1
    bundle = segments[0]
    # The shared read-only bundle carries valid and invalidated entries,
    # conflicts, gaps and source references for every perspective.
    assert '"effective_entries":[' in bundle
    assert '"invalidated_entries":[' in bundle
    assert '"old-fact-1"' in bundle
    assert '"conflicts":[' in bundle
    assert '"conflict-1"' in bundle
    assert '"evidence_gaps":' in bundle
    assert '"source_ids":["source-1"]' in bundle
    assert '"quote":"The agency narrowed the scope."' in bundle


def test_evidence_urls_survive_sanitization_while_absolute_paths_do_not(tmp_path):
    quote = (
        "The agency narrowed the scope (reported at "
        "https://example.com/story/2) and /home/user/notes.md records the detail."
    )
    custom = EvolutionAnalysis(
        case_id=CASE_ID,
        as_of=AS_OF,
        case_type="policy",
        stages=(stage("case-1", "evolution_case", source_ids=()), stage("node-1", quote=quote)),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
        invalidated_stages=(),
    )

    class PromptRouter(ScriptedRouter):
        def __init__(self):
            super().__init__()
            self.prompts = []

        async def complete(self, role, prompt):
            self.prompts.append(prompt)
            return await super().complete(role, prompt)

    router = PromptRouter()
    run(service(tmp_path, router, FakeAnalyzer(custom)).debate(CASE_ID, QUESTION, AS_OF))

    assert router.prompts
    for prompt in router.prompts:
        assert "https://example.com/story/2" in prompt
        assert "/home/user/notes.md" not in prompt


def test_perspectives_arguments_are_type_checked_before_use(tmp_path):
    debate_service = service(tmp_path, ScriptedRouter())

    with pytest.raises(TypeError):
        run(debate_service.debate(CASE_ID, QUESTION, AS_OF, perspectives="academic_observer"))

    with pytest.raises(ValueError) as error:
        run(debate_service.debate(CASE_ID, QUESTION, AS_OF, perspectives=("ghost",)))
    assert "institutional_regulatory" in str(error.value)


def test_unknown_configured_profile_id_lists_valid_profiles(tmp_path):
    from prism.debate import DebateService

    with pytest.raises(ValueError) as error:
        DebateService(FakeAnalyzer(), None, profiles=("bogus-1", "bogus-2", "bogus-3"))
    assert "institutional_regulatory" in str(error.value)
    assert "KeyError" not in str(error.value)


def test_missing_debate_role_route_degrades_with_full_audit(tmp_path):
    from prism.llm import MissingRoleError
    from prism.debate import DebateLedger

    class NoRoleRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, role, prompt):
            self.calls += 1
            raise MissingRoleError("missing task role: 'debate'")

    router = NoRoleRouter()
    db = tmp_path / "index.db"
    result = run(
        service(db.parent, router).debate(CASE_ID, QUESTION, AS_OF)
    )

    assert router.calls == 4
    assert result.status == "no_conclusion"
    assert result.synthesis is None
    assert result.fallback_reason == (
        "no debate conclusion: all perspective LLM calls unavailable"
    )
    assert {item.error_code for item in result.errors} == {"llm_unavailable"}
    history = DebateLedger(db).entries(CASE_ID)
    assert len(history) == 1
    assert history[0].status == "no_conclusion"


def test_api_debate_case_persists_the_audit_ledger(tmp_path, monkeypatch):
    from prism.api import PrismAPI
    from prism.debate import DebateLedger, DebateService
    from prism.events import Event
    from prism.ingestion import IngestionResult
    from prism.domain import Material

    monkeypatch.setenv("PRISM_HOME", str(tmp_path))
    material = Material(
        id="m-1", title="t", source="s", published_at=AS_OF,
        fetched_at=AS_OF, type="policy", content="content",
    )

    class Ingestion:
        def ingest(self, path, metadata=None):
            return IngestionResult(material, Path("raw.md"), Path("corpus/m.md"), False, "direct")

    class Store:
        def index_file(self, path):
            return None

        def search(self, criteria, *, limit, offset):
            return []

    class Graph:
        async def timeline(self, case_id, as_of):
            return None

        async def add_case(self, case, **bundle):
            return None

    class Bus:
        async def publish(self, event: Event):
            pass

    db = tmp_path / "index.db"
    debate = DebateService(FakeAnalyzer(), None, ledger=DebateLedger(db))
    api = PrismAPI(Ingestion(), Store(), Graph(), Bus(), debate_service=debate)

    result = run(api.debate_case(CASE_ID, QUESTION, AS_OF, perspectives=("academic_observer",)))
    assert result.status == "no_conclusion"

    history = DebateLedger(db).entries(CASE_ID)
    assert len(history) == 1
    assert history[0].question == QUESTION
    assert history[0].profiles == ("academic_observer",)
    assert history[0].status == "no_conclusion"
    assert history[0].fallback_reason == "debate LLM role is unavailable"


def _cutoff_analysis(cutoff: datetime, case_type: str = "policy") -> EvolutionAnalysis:
    return EvolutionAnalysis(
        case_id=CASE_ID,
        as_of=cutoff,
        case_type=case_type,
        stages=(
            stage("case-1", "evolution_case", source_ids=(), valid_at=cutoff),
            stage("node-1", valid_at=cutoff),
            stage("claim-1", "claim", layer="interpretation", valid_at=cutoff),
        ),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
        invalidated_stages=(),
    )


class CutoffAnalyzer(FakeAnalyzer):
    """Honor the requested cutoff instead of returning one fixed snapshot."""

    def __init__(self, case_type: str = "policy"):
        super().__init__(_cutoff_analysis(AS_OF, case_type))
        self.case_type = case_type

    async def analyze(self, case_id, as_of=None, *, kinds=None):
        self.calls.append((case_id, as_of))
        return _cutoff_analysis(as_of if as_of is not None else AS_OF, self.case_type)


class PromptCapture(ScriptedRouter):
    def __init__(self, independent=None, cross=None, synthesis=None):
        super().__init__(independent=independent, cross=cross, synthesis=synthesis)
        self.prompts = []

    async def complete(self, role, prompt):
        self.prompts.append(prompt)
        return await super().complete(role, prompt)


def test_default_profiles_follow_the_real_case_type_taxonomy(tmp_path):
    from prism.debate import ACADEMIC_PROFILES, DEFAULT_PROFILES, DebateService

    academic_ids = tuple(profile.id for profile in ACADEMIC_PROFILES)
    default_ids = tuple(profile.id for profile in DEFAULT_PROFILES)
    expected = {
        "academic_discourse": academic_ids,
        "academic": academic_ids,
        "policy": default_ids,
        "public_issue": default_ids,
        None: default_ids,
    }
    for index, (case_type, wanted) in enumerate(expected.items()):
        # A fresh question per case type keeps each run off the replay path.
        fake = CutoffAnalyzer(case_type) if case_type is not None else FakeAnalyzer(
            _cutoff_analysis(AS_OF, None)
        )
        result = run(
            DebateService(fake, None, ledger=None).debate(
                CASE_ID, f"Q-{index}-{case_type}", AS_OF
            )
        )
        assert result.profiles == wanted
        assert result.status == "no_conclusion"


def test_duplicate_requested_perspectives_are_rejected(tmp_path):
    debate_service = service(tmp_path, ScriptedRouter())
    with pytest.raises(ValueError) as error:
        run(
            debate_service.debate(
                CASE_ID,
                QUESTION,
                AS_OF,
                perspectives=("academic_observer", "academic_observer"),
            )
        )
    assert "duplicate" in str(error.value).lower()


def test_empty_cross_challenges_are_legitimate(tmp_path):
    router = ScriptedRouter(cross={"challenges": []})
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

    assert result.status == "completed"
    assert result.fallback_reason is None
    assert all(item.status == "available" for item in result.results)
    assert all(item.cross_examination is not None for item in result.results)
    assert all(item.cross_examination.challenges == () for item in result.results)
    assert result.synthesis is not None
    assert len(router.calls) == 9


def test_empty_statements_are_rejected_as_invalid_output(tmp_path):
    router = ScriptedRouter(independent='{"statements": []}')
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

    assert result.status == "no_conclusion"
    assert all(item.status == "unavailable" for item in result.results)
    assert result.errors
    assert {item.error_code for item in result.errors} == {"invalid_output"}
    assert all(
        item.failure.phase == "independent"
        for item in result.results
        if item.failure is not None
    )


def test_invalid_output_in_one_perspective_is_isolated(tmp_path):
    class PartialInvalidRouter(PromptCapture):
        async def complete(self, role, prompt):
            current = perspective(prompt)
            if phase(prompt) == "independent" and current == "institutional_regulatory":
                return Completion(text="not json", provider="offline", model="test")
            return await super().complete(role, prompt)

    router = PartialInvalidRouter()
    result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

    assert result.status == "completed_with_unavailable_perspectives"
    failed = result.results[0]
    assert failed.status == "unavailable"
    assert failed.failure is not None
    assert failed.failure.phase == "independent"
    assert failed.failure.error_code == "invalid_output"
    assert failed.interpretation is None
    assert [item.status for item in result.results[1:]] == ["available"] * 3
    assert len(result.errors) == 1
    # The failed perspective's statements never reach the other perspectives'
    # cross-examination or the synthesis input, so no one can cite them.
    assert all(
        "institutional-fact" not in prompt
        for prompt in router.prompts
        if phase(prompt) == "cross_examination"
    )
    assert any(
        "industry-fact" in prompt
        for prompt in router.prompts
        if phase(prompt) == "cross_examination"
    )


def test_as_of_cutoffs_produce_distinct_non_replayed_runs(tmp_path):
    db = tmp_path / "index.db"
    first = run(
        service(db.parent, ScriptedRouter(), CutoffAnalyzer()).debate(
            CASE_ID, QUESTION, AS_OF
        )
    )
    earlier = run(
        service(db.parent, ScriptedRouter(), CutoffAnalyzer()).debate(
            CASE_ID, QUESTION, EARLY
        )
    )
    assert first.replayed is False
    assert earlier.replayed is False
    assert first.as_of == AS_OF
    assert earlier.as_of == EARLY
    assert first.evidence_bundle_hash != earlier.evidence_bundle_hash

    from prism.debate import DebateLedger

    history = DebateLedger(db).entries(CASE_ID)
    assert len(history) == 2
    assert len({entry.input_hash for entry in history}) == 2

    # Re-running the earlier cutoff replays that exact run, not the newest.
    router = ScriptedRouter()
    replayed = run(
        service(db.parent, router, CutoffAnalyzer()).debate(CASE_ID, QUESTION, EARLY)
    )
    assert replayed.replayed is True
    assert replayed.as_of == EARLY
    assert replayed.evidence_bundle_hash == earlier.evidence_bundle_hash
    assert router.calls == []


def test_evidence_bundle_hash_is_stable_for_an_identical_snapshot(tmp_path):
    db = tmp_path / "index.db"
    fake = CutoffAnalyzer()
    one = run(service(db.parent, ScriptedRouter(), fake).debate(CASE_ID, "Q one", AS_OF))
    two = run(service(db.parent, ScriptedRouter(), fake).debate(CASE_ID, "Q two", AS_OF))

    assert one.evidence_bundle_hash == two.evidence_bundle_hash

    from prism.debate import DebateLedger

    history = DebateLedger(db).entries(CASE_ID)
    assert len(history) == 2
    assert len({entry.evidence_bundle_hash for entry in history}) == 1
    assert len({entry.input_hash for entry in history}) == 2


def test_result_dict_roundtrip_and_strict_field_contract(tmp_path):
    from prism.debate import result_from_dict, result_to_dict

    result = run(service(tmp_path, ScriptedRouter()).debate(CASE_ID, QUESTION, AS_OF))
    assert result.status == "completed"

    payload = result_to_dict(result)
    roundtrip = result_from_dict(json.loads(json.dumps(payload)))
    assert roundtrip == result
    assert roundtrip.replayed is result.replayed

    missing = dict(payload)
    del missing["synthesis"]
    with pytest.raises(ValueError, match="missing"):
        result_from_dict(missing)

    unknown = dict(payload)
    unknown["bogus_field"] = True
    with pytest.raises(ValueError, match="unknown"):
        result_from_dict(unknown)

    corrupt_statement = json.loads(json.dumps(payload))
    statement = corrupt_statement["results"][0]["interpretation"]["statements"][0]
    statement["unexpected"] = True
    with pytest.raises((TypeError, ValueError)):
        result_from_dict(corrupt_statement)


def test_ledger_record_is_idempotent_across_restarts(tmp_path):
    from prism.debate import DebateLedger, DebateService

    db = tmp_path / "index.db"
    ledger = DebateLedger(db)
    result = run(
        DebateService(FakeAnalyzer(), ScriptedRouter(), ledger=None).debate(
            CASE_ID, QUESTION, AS_OF
        )
    )
    assert ledger.record(result, [], "hash-1") is result
    # The same immutable input recorded twice must still leave one row.
    ledger.record(result, [], "hash-1")
    assert len(ledger.entries(CASE_ID)) == 1

    reopened = DebateLedger(db)
    assert len(reopened.entries(CASE_ID)) == 1
    assert reopened.entries(CASE_ID)[0].input_hash == "hash-1"
    # A different input hash records a separate row and never overwrites.
    reopened.record(result, [], "hash-2")
    assert len(reopened.entries(CASE_ID)) == 2


def test_evidence_quotes_are_scrubbed_from_every_prompt(tmp_path):
    quote = (
        f"The agency narrowed the scope using {SECRET} and kept notes at "
        f"{ABSOLUTE_PATH}; see https://example.com/story/2 for the report."
    )
    custom = EvolutionAnalysis(
        case_id=CASE_ID,
        as_of=AS_OF,
        case_type="policy",
        stages=(
            stage("case-1", "evolution_case", source_ids=()),
            stage("node-1", quote=quote),
        ),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
        invalidated_stages=(),
    )
    router = PromptCapture()
    run(service(tmp_path, router, FakeAnalyzer(custom)).debate(CASE_ID, QUESTION, AS_OF))

    assert router.prompts
    for prompt in router.prompts:
        assert SECRET not in prompt
        assert ABSOLUTE_PATH not in prompt
        assert "https://example.com/story/2" in prompt
        assert "[REDACTED]" in prompt


def test_synthesis_invalid_outputs_degrade_without_failing_the_case(tmp_path):
    valid = synthesis_payload()
    invalid_cases = [
        # Unknown evidence id in a consensus point.
        json.dumps({**valid, "consensus": [
            {"text": "cites a ghost", "evidence_ids": ["ghost"]}
        ]}),
        # Unknown top-level field.
        json.dumps({**valid, "extra_field": 1}),
        # Duplicate JSON key in the payload.
        json.dumps(valid).replace(
            '"consensus":[', '"consensus":[],"consensus":[', 1
        ),
        # NaN is not a valid text value.
        json.dumps(valid).replace(
            '"text":"The recorded evidence says the scope narrowed."', '"text":NaN', 1
        ),
    ]
    for text in invalid_cases:
        router = ScriptedRouter(synthesis=text)
        result = run(service(tmp_path, router).debate(CASE_ID, QUESTION, AS_OF))

        assert result.status == "degraded"
        assert result.fallback_reason == (
            "synthesis invalid; deterministic conservative summary used"
        )
        assert result.synthesis is not None
        assert result.synthesis.consensus == ()
        assert result.synthesis.disagreements == ()
        assert len(result.synthesis.key_evidence) == 1
        assert len(result.synthesis.unresolved_questions) == 1
        assert result.synthesis.unresolved_questions[0].evidence_ids == (
            result.synthesis.key_evidence[0].evidence_id,
        )
        assert result.errors[-1].phase == "synthesis"
        assert result.errors[-1].error_code == "invalid_output"
        assert all(item.status == "available" for item in result.results)
