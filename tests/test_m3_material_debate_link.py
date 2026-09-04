"""M3 slice: append material -> changed GTI snapshot -> affected debate context.

FR-5.7 (append material at a discussion time point) meets FR-6.7 (saved
report versions): one ``add_material`` call optionally links to an immutable
parent debate run, validates the parent durably, and exposes the prior and
current evidence-bundle hashes so the caller can start an explicit new debate
or named follow-up. No LLM call is ever made to detect staleness and no
debate is rerun automatically.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path

import pytest

from prism.analyzer import EvolutionAnalysis, TimelineStage
from prism.api import PrismAPI
from prism.api.facade import ProcessMaterialResult
from prism.cli import build_parser, handle_add_material, main
from prism.config import PathConfig, PrismConfig
from prism.debate import DebateLedger, DebateService
from prism.domain import (
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
)
from prism.ingestion import IngestionResult
from prism.pipeline import PipelineError
from prism.report.ledger import ReportVersionLedger
from prism.runtime import create_runtime


UTC = timezone.utc
PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
AS_OF_LATER = datetime(2026, 9, 2, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)


def make_case(case_id: str = "case-b") -> EvolutionCase:
    return EvolutionCase(
        case_id=case_id,
        case_type="policy",
        canonical_name=f"Case {case_id}",
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="active",
    )


def make_material(material_id: str = "mat-1") -> Material:
    return Material(
        id=material_id,
        title=f"Material {material_id}",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content="The agency published the revised policy.",
        case_tags=("case-b",),
    )


def make_analysis(
    case_id: str = "case-b",
    as_of: datetime = AS_OF,
    *,
    summary: str = "The policy was published.",
) -> EvolutionAnalysis:
    stage = TimelineStage(
        episode_key="node-1",
        kind="evolution_node",
        layer="fact",
        summary=summary,
        valid_at=PUBLISHED,
        invalid_at=None,
        reference_time=PUBLISHED,
        source_ids=("mat-1",),
    )
    return EvolutionAnalysis(
        case_id=case_id,
        as_of=as_of,
        case_type="policy",
        stages=(stage,),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
    )


def make_link(parent_run_id: str = "run-1"):
    from prism.api.facade import MaterialDebateLink

    return MaterialDebateLink(
        parent_run_id=parent_run_id,
        case_id="case-b",
        as_of=AS_OF,
        prior_evidence_bundle_hash="a" * 64,
        current_evidence_bundle_hash="b" * 64,
        affected=True,
        stale=True,
    )


def pipeline_run():
    @dataclass
    class Run:
        status: str = "completed"
        stages: tuple = ()

    return Run()


def pipeline_result(link: object | None = None) -> ProcessMaterialResult:
    return ProcessMaterialResult(
        material_id="mat-new",
        pipeline=pipeline_run(),
        case_id="case-b",
        case_outcome=None,
        debate_link=link,
    )


class StubIngestion:
    def ingest(self, path, metadata=None):
        return IngestionResult(
            material=make_material("mat-new"),
            raw_path=Path("raw/input.md"),
            corpus_path=Path("corpus/input.md"),
            used_ocr=False,
            extracted_via="test",
        )


class StubStore:
    def index_file(self, path):
        return object()

    def search(self, criteria, *, limit=50, offset=0):
        return []

    def get(self, source_id):
        return object()


class StubGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("add_material must not read the graph directly")

    async def add_case(self, case, **bundle):
        raise AssertionError("add_material must not write the graph directly")


class StubBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class FakeAnalyzer:
    def __init__(self, analyses):
        self.analyses = list(analyses)
        self.calls = []

    async def analyze(self, case_id, as_of=None, *, kinds=None):
        self.calls.append((case_id, as_of))
        return self.analyses.pop(0)


class StaticAnalyzer:
    def __init__(self, analysis):
        self.analysis = analysis

    async def analyze(self, case_id, as_of=None, *, kinds=None):
        return self.analysis


class FakeReportService:
    def __init__(self):
        self.calls = []

    async def report(self, analysis, debate_result=None):
        self.calls.append((analysis, debate_result))
        raise AssertionError("use_llm=False must never call the report LLM service")


class FakeCaseService:
    async def merge_case(self, case_id):
        return None

    async def record_extraction(self, material, extraction):
        return None

    def load_case(self, case_id):
        return make_case(case_id)

    def case_for_material(self, material_id):
        return "case-b"


class FakePipeline:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def run_material(self, result, *, target_case=None):
        self.calls.append((result, target_case))
        if self.error is not None:
            raise self.error
        return self.result

    def case_outcome_for(self, material_id):
        return type("Outcome", (), {"case_id": "case-b", "warnings": ()})()

    def run_for(self, material_id):
        return self.result


class FakeRouter:
    """A router whose completions satisfy every debate phase."""

    def __init__(self):
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        if '"phase":"independent"' in prompt:
            return _completion({
                "statements": [
                    {"id": "fact-1", "classification": "fact",
                     "text": "The policy was published.",
                     "evidence_ids": ["node-1"]},
                ]
            })
        if '"phase":"cross_examination"' in prompt:
            return _completion({"challenges": []})
        return _completion({
            "consensus": [], "disagreements": [], "sources_of_disagreement": [],
            "key_evidence": [], "unresolved_questions": [],
            "falsification_conditions": [],
        })


def _completion(payload):
    from prism.llm import Completion

    return Completion(
        json.dumps(payload), provider="test", model="test"
    )


def make_linked_api(
    tmp_path: Path,
    analyses,
    *,
    pipeline: FakePipeline | None = None,
    parent_case: str = "case-b",
    parent_as_of: datetime = AS_OF,
):
    """Build an API whose debate parent is durably recorded first.

    The parent debate runs through a real DebateService and DebateLedger with
    a working router, so ``parent.evidence_bundle_hash`` is exactly the hash
    of the parent analysis payload. The facade and the debate service share
    one analyzer so report saves and staleness recomputation read the same
    synthetic evidence.
    """
    ledger = DebateLedger(tmp_path / "debate.db")
    router = FakeRouter()
    parent_analysis = make_analysis(parent_case, parent_as_of)
    parent_service = DebateService(
        StaticAnalyzer(parent_analysis), router, ledger=ledger
    )
    parent = run(
        parent_service.debate(
            parent_case,
            "What changed?",
            parent_as_of,
            perspectives=("institutional_regulatory",),
        )
    )
    parent_id = ledger.entries(parent_case)[0].run_id
    analyzer = FakeAnalyzer(list(analyses))
    debate = DebateService(analyzer, router, ledger=ledger)
    api = PrismAPI(
        StubIngestion(),
        StubStore(),
        StubGraph(),
        StubBus(),
        analyzer_service=analyzer,
        report_service=FakeReportService(),
        pipeline_service=pipeline if pipeline is not None else FakePipeline(
            pipeline_run()
        ),
        case_service=FakeCaseService(),
        report_version_service=ReportVersionLedger(make_paths(tmp_path)),
        debate_service=debate,
        debate_ledger=ledger,
    )
    return api, ledger, parent, parent_id, analyzer, router


def test_add_material_without_parent_keeps_legacy_output(tmp_path):
    analysis = make_analysis()
    analyzer = FakeAnalyzer([analysis])
    api = PrismAPI(
        StubIngestion(),
        StubStore(),
        StubGraph(),
        StubBus(),
        analyzer_service=analyzer,
        report_service=FakeReportService(),
        pipeline_service=FakePipeline(pipeline_run()),
        case_service=FakeCaseService(),
        report_version_service=ReportVersionLedger(make_paths(tmp_path)),
    )

    result = run(api.add_material("input.md", "case-b", as_of=AS_OF, use_llm=False))

    assert result.material_id == "mat-new"
    assert result.report_version is not None
    assert result.report_version.trigger == "material_added"
    assert result.debate_link is None
    assert analyzer.calls == [("case-b", AS_OF)]
    assert len(api._report_version_service.versions("case-b")) == 1


def test_add_material_links_parent_when_evidence_unchanged(tmp_path):
    analysis = make_analysis()
    # One analysis for the initial version save, then the add_material report
    # save and the staleness recomputation read the same unchanged evidence.
    api, ledger, parent, parent_id, analyzer, router = make_linked_api(
        tmp_path, [analysis, analysis, analysis]
    )
    initial = run(
        api.save_report_version("case-b", AS_OF, use_llm=False, trigger="initial")
    )
    router_calls = len(router.calls)

    result = run(
        api.add_material(
            "input.md",
            "case-b",
            parent_debate_run_id=parent_id,
            use_llm=False,
        )
    )

    link = result.debate_link
    assert link is not None
    assert link.parent_run_id == parent_id
    assert link.case_id == "case-b"
    assert link.as_of == parent.as_of == AS_OF
    assert link.prior_evidence_bundle_hash == parent.evidence_bundle_hash
    assert link.current_evidence_bundle_hash == parent.evidence_bundle_hash
    assert link.affected is False
    assert link.stale is False
    # Unchanged evidence at the cutoff deduplicates to the initial version.
    assert result.report_version == initial
    assert api._report_version_service.versions("case-b") == (initial,)
    # One pre-save analysis, then the add_material report save and the
    # staleness recomputation at the same cutoff.
    assert analyzer.calls == [("case-b", AS_OF)] * 3
    # Staleness detection never touches the LLM router.
    assert len(router.calls) == router_calls


def test_add_material_marks_parent_stale_when_evidence_changed(tmp_path):
    initial_analysis = make_analysis()
    changed_analysis = make_analysis(
        summary="A second material changed the recorded evidence."
    )
    api, ledger, parent, parent_id, analyzer, router = make_linked_api(
        tmp_path, [initial_analysis, changed_analysis, changed_analysis]
    )
    initial = run(
        api.save_report_version("case-b", AS_OF, use_llm=False, trigger="initial")
    )
    router_calls = len(router.calls)

    result = run(
        api.add_material(
            "input.md",
            "case-b",
            as_of=AS_OF,
            parent_debate_run_id=parent_id,
            use_llm=False,
        )
    )

    link = result.debate_link
    assert link is not None
    assert link.prior_evidence_bundle_hash == parent.evidence_bundle_hash
    assert link.current_evidence_bundle_hash != parent.evidence_bundle_hash
    assert link.affected is True
    assert link.stale is True
    # The material_added version is saved at the parent cutoff.
    version = result.report_version
    assert version is not None and version != initial
    assert version.trigger == "material_added"
    assert version.as_of == AS_OF
    assert [
        item.trigger for item in api._report_version_service.versions("case-b")
    ] == ["initial", "material_added"]
    # No debate is rerun automatically: the router saw no new call.
    assert len(router.calls) == router_calls
    # The parent debate run stays immutable and readable.
    durable = ledger.result_by_run_id(parent_id)
    assert durable is not None
    assert durable.case_id == parent.case_id
    assert durable.as_of == parent.as_of
    assert durable.question == parent.question
    assert durable.evidence_bundle_hash == parent.evidence_bundle_hash
    assert len(ledger.entries("case-b")) == 1


def test_add_material_parent_validation_fails_before_processing_or_report(tmp_path):
    # Unknown parent run id.
    api, _, parent, parent_id, analyzer, _ = make_linked_api(
        tmp_path, [make_analysis()]
    )
    with pytest.raises(LookupError):
        run(
            api.add_material(
                "input.md",
                "case-b",
                parent_debate_run_id="missing-run",
                use_llm=False,
            )
        )
    assert api._pipeline.calls == []
    assert analyzer.calls == []
    assert api._report_version_service.versions() == ()

    # Parent debates another case than the declared target case.
    other_tmp = tmp_path / "case-mismatch"
    other_tmp.mkdir()
    api, _, _, _, analyzer, _ = make_linked_api(
        other_tmp, [make_analysis()], parent_case="case-a"
    )
    with pytest.raises(ValueError, match="case"):
        run(
            api.add_material(
                "input.md",
                "case-b",
                parent_debate_run_id=next(
                    iter(api._debate_ledger.entries("case-a"))
                ).run_id,
                use_llm=False,
            )
        )
    assert api._pipeline.calls == []
    assert analyzer.calls == []
    assert api._report_version_service.versions() == ()

    # Requested as_of does not match the parent cutoff.
    api, _, parent, parent_id, analyzer, _ = make_linked_api(
        tmp_path / "as-of", [make_analysis()]
    )
    with pytest.raises(ValueError, match="as_of"):
        run(
            api.add_material(
                "input.md",
                "case-b",
                as_of=AS_OF_LATER,
                parent_debate_run_id=parent_id,
                use_llm=False,
            )
        )
    # A naive cutoff can never match the parent's aware cutoff.
    with pytest.raises(ValueError, match="as_of"):
        run(
            api.add_material(
                "input.md",
                "case-b",
                as_of=datetime(2026, 9, 1),
                parent_debate_run_id=parent_id,
                use_llm=False,
            )
        )
    assert api._pipeline.calls == []
    assert analyzer.calls == []
    assert api._report_version_service.versions() == ()


def test_add_material_snapshot_refresh_failure_writes_no_report_version(tmp_path):
    analysis = make_analysis()
    api, _, _, parent_id, _, _ = make_linked_api(tmp_path, [analysis])

    class FailingDebate:
        async def evidence_bundle_hash(self, case_id, as_of):
            raise RuntimeError("GTI snapshot unavailable")

    api._debate = FailingDebate()
    with pytest.raises(RuntimeError, match="GTI snapshot unavailable"):
        run(
            api.add_material(
                "input.md", "case-b", parent_debate_run_id=parent_id, use_llm=False
            )
        )

    assert api._report_version_service.versions("case-b") == ()



def test_add_material_pipeline_failure_writes_no_report_version_with_parent(tmp_path):
    pipeline = FakePipeline(
        error=PipelineError("extract failed", stage="extract", material_id="mat-new")
    )
    api, _, parent, parent_id, analyzer, router = make_linked_api(
        tmp_path / "failure", [make_analysis()], pipeline=pipeline
    )
    router_calls = len(router.calls)

    with pytest.raises(PipelineError):
        run(
            api.add_material(
                "input.md",
                "case-b",
                parent_debate_run_id=parent_id,
                use_llm=False,
            )
        )

    # The material was processed (once) but nothing was rendered or saved.
    assert len(pipeline.calls) == 1
    assert analyzer.calls == []
    assert api._report_version_service.versions() == ()
    assert len(router.calls) == router_calls


def test_add_material_parent_requires_debate_dependencies(tmp_path):
    api = PrismAPI(
        StubIngestion(),
        StubStore(),
        StubGraph(),
        StubBus(),
        analyzer_service=FakeAnalyzer([make_analysis()]),
        report_service=FakeReportService(),
        pipeline_service=FakePipeline(pipeline_run()),
        case_service=FakeCaseService(),
        report_version_service=ReportVersionLedger(make_paths(tmp_path)),
    )
    with pytest.raises(ValueError, match="debate_ledger"):
        run(
            api.add_material(
                "input.md",
                "case-b",
                parent_debate_run_id="any-run",
                use_llm=False,
            )
        )
    assert api._pipeline.calls == []


def run_cli(argv, api):
    stdout = StringIO()
    stderr = StringIO()
    status = run(main(argv, api=api, stdout=stdout, stderr=stderr))
    return status, stdout.getvalue(), stderr.getvalue()


class ParentRecordingCLIAPI:
    def __init__(self):
        self.calls = []

    async def add_material(
        self, source, target_case, metadata=None, as_of=None, use_llm=True,
        parent_debate_run_id=None,
    ):
        self.calls.append(
            (
                "add_material",
                (source, target_case, metadata, as_of, use_llm),
                {"parent_debate_run_id": parent_debate_run_id},
            )
        )
        return pipeline_result(make_link(parent_debate_run_id))


class LegacyCLIAPI:
    """An older facade implementation without the new keyword."""

    def __init__(self):
        self.calls = []

    async def add_material(self, source, target_case, metadata=None, as_of=None,
                           use_llm=True):
        self.calls.append(("add_material", (source, target_case, metadata, as_of, use_llm), {}))
        return pipeline_result()


def test_cli_add_material_delegates_parent_link_and_renders_json():
    args = build_parser().parse_args(
        [
            "add-material",
            "input.md",
            "--case-id",
            "case-b",
            "--parent-debate-run",
            "run-1",
        ]
    )
    assert args.handler is handle_add_material
    assert args.parent_debate_run == "run-1"
    plain = build_parser().parse_args(
        ["add-material", "input.md", "--case-id", "case-b"]
    )
    assert plain.parent_debate_run is None

    api = ParentRecordingCLIAPI()
    status, stdout, stderr = run_cli(
        [
            "add-material",
            "input.md",
            "--case-id",
            "case-b",
            "--parent-debate-run",
            "run-1",
            "--no-llm",
        ],
        api,
    )
    assert status == 0 and stderr == ""
    assert api.calls == [
        (
            "add_material",
            ("input.md", "case-b", None, None, False),
            {"parent_debate_run_id": "run-1"},
        )
    ]
    payload = json.loads(stdout)
    assert payload["material_id"] == "mat-new"
    assert payload["debate_link"]["parent_run_id"] == "run-1"
    assert payload["debate_link"]["case_id"] == "case-b"
    assert payload["debate_link"]["as_of"] == AS_OF.isoformat()
    assert payload["debate_link"]["prior_evidence_bundle_hash"] == "a" * 64
    assert payload["debate_link"]["current_evidence_bundle_hash"] == "b" * 64
    assert payload["debate_link"]["affected"] is True
    assert payload["debate_link"]["stale"] is True

    # Without the flag the CLI must not force the new keyword on old callers.
    legacy = LegacyCLIAPI()
    status, _, stderr = run_cli(
        ["add-material", "input.md", "--case-id", "case-b", "--no-llm"], legacy
    )
    assert status == 0 and stderr == ""
    assert legacy.calls == [
        ("add_material", ("input.md", "case-b", None, None, False), {})
    ]


class FakeGraphBackend:
    def __init__(self):
        self.episodes = {}

    async def add_episode(self, episode):
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query):
        return tuple(self.episodes.values())


class TargetBoundExtractor:
    name = "target-bound"

    async def extract(self, material):
        return await self.extract_material(material)

    async def extract_material(self, material, *, corpus_path=None, target_case=None):
        locator = EvidenceLocator(
            source_id=material.id,
            corpus_path=f"corpus/2026-08/example.test/{material.id}.md",
            paragraph=1,
            quote=material.content,
        )
        node = EvolutionNode(
            id=f"node-{material.id}",
            case_id=target_case.case_id,
            node_type="publication",
            happened_at=material.published_at,
            summary=material.title,
            source_ids=(material.id,),
            valid_at=material.published_at,
            observed_at=material.published_at,
            evidence=(locator,),
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
        from prism.extraction import ExtractionResult

        return ExtractionResult(
            case=target_case, nodes=(node,), temporal_facts=(fact,)
        )


DOC_TEMPLATE = """---
source: example.test
title: {title}
published_at: 2026-08-30T09:00:00+00:00
fetched_at: 2026-08-31T12:00:00+00:00
type: policy
case_tags: ["case-b"]
access_level: fulltext
---

{body}
"""


def write_material(home: Path, name: str, body: str) -> Path:
    directory = home / "corpus" / "2026-08" / "example.test"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.md"
    target.write_text(DOC_TEMPLATE.format(title=name, body=body), encoding="utf-8")
    return target


def test_runtime_material_debate_link_survives_restart(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("PRISM_HOME", str(tmp_path))
        config_path = tmp_path / "config.json"
        PrismConfig().save(config_path)
        runtime = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=TargetBoundExtractor(),
        )
        try:
            first = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            processed = await runtime.api.process_material(
                first, target_case=make_case("case-b")
            )
            assert processed.case_id == "case-b"

            parent = await runtime.api.debate_case("case-b", "What changed?", AS_OF)
            parent_id = runtime.debate_ledger.entries("case-b")[0].run_id

            second = write_material(
                tmp_path,
                "policy-reaction",
                "Analysts responded to the revised policy.",
            )
            added = await runtime.api.add_material(
                second,
                "case-b",
                parent_debate_run_id=parent_id,
                use_llm=False,
            )
            link = added.debate_link
            assert link.parent_run_id == parent_id
            assert link.case_id == "case-b"
            assert link.as_of == AS_OF
            assert link.prior_evidence_bundle_hash == parent.evidence_bundle_hash
            assert link.current_evidence_bundle_hash != parent.evidence_bundle_hash
            assert link.affected is True
            assert link.stale is True
            assert added.report_version.trigger == "material_added"
            assert added.report_version.as_of == AS_OF

            durable = runtime.debate_ledger.result_by_run_id(parent_id)
            assert durable is not None
            assert durable.evidence_bundle_hash == parent.evidence_bundle_hash
            assert durable.as_of == parent.as_of
            assert len(runtime.debate_ledger.entries("case-b")) == 1
        finally:
            await runtime.close()

        restarted = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=TargetBoundExtractor(),
        )
        try:
            # The parent run id resolves again from the durable ledger.
            third = write_material(
                tmp_path,
                "policy-implementation",
                "Implementation of the revised policy began.",
            )
            again = await restarted.api.add_material(
                third,
                "case-b",
                parent_debate_run_id=parent_id,
                use_llm=False,
            )
            assert again.debate_link.parent_run_id == parent_id
            assert (
                again.debate_link.prior_evidence_bundle_hash
                == parent.evidence_bundle_hash
            )
            assert again.debate_link.stale is True
            assert again.report_version.trigger == "material_added"
        finally:
            await restarted.close()

    run(scenario())
