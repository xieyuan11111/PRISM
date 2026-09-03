"""End-to-end explicit target-case context across the processing layers.

The caller declares one ``EvolutionCase`` for a material; the pipeline passes
that context to the extractor, the case service loads real recorded cases by
id (never guessed from titles or tags), the facade and CLI forward it, and the
durable one-material-one-case gate still refuses cross-case writes.  All tests
are offline; no real materials, LLMs or services are used.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pytest

from prism.cases import CaseBundleMerger, CaseExtractionLedger, CaseService, MaterialCaseConflict
from prism.config import PathConfig, PrismConfig
from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
)
from prism.extraction import ExtractionResult
from prism.graph import GraphEpisode, GraphWriteResult
from prism.pipeline import PipelineError, PipelineService
from prism.runtime import PrismRuntime, create_runtime


UTC = timezone.utc
PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
TARGET_CASE_ID = "case-target-1"


def run(coro):
    return asyncio.run(coro)


def target_case(**overrides) -> EvolutionCase:
    values = dict(
        case_id=TARGET_CASE_ID,
        case_type="policy",
        canonical_name="Declared target policy",
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="active",
    )
    values.update(overrides)
    return EvolutionCase(**values)


def make_material(material_id: str = "mat-1", **overrides) -> Material:
    values = dict(
        id=material_id,
        title="Policy update",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content="The agency published the revised policy.",
        case_tags=(TARGET_CASE_ID,),
    )
    values.update(overrides)
    return Material(**values)


def make_result(material_id: str = "mat-1") -> object:
    from prism.ingestion import IngestionResult

    material = make_material(material_id)
    return IngestionResult(
        material=material,
        raw_path=Path(f"raw/{material_id}.pdf"),
        corpus_path=Path(f"corpus/2026-08/example.test/{material_id}.md"),
        used_ocr=False,
        extracted_via="md",
    )


# ------------------------------------------------------------------- pipeline


class _Indexer:
    def __init__(self) -> None:
        self.calls = []

    def index_file(self, path) -> object:
        from prism.store import IndexEntry, IndexOutcome

        self.calls.append(path)
        entry = IndexEntry(
            source_id="mat-1",
            title="Policy update",
            source="example.test",
            published_at=PUBLISHED,
            fetched_at=FETCHED,
            type="policy",
            content="The agency published the revised policy.",
            path=str(path),
            content_hash="hash",
            case_tags=(TARGET_CASE_ID,),
        )
        return IndexOutcome(entry=entry, status="indexed")


class _Graph:
    def __init__(self) -> None:
        self.calls = []

    async def add_case(self, case, *, nodes=(), facts=(), claims=(), materials=()):
        self.calls.append((case, nodes))
        return GraphWriteResult((), ("merged-episode",), ())


class _Clock:
    def __init__(self) -> None:
        self.value = PUBLISHED

    def __call__(self) -> datetime:
        return self.value


class _CaseRecorder:
    """Case-service stand-in recording what the pipeline hands it."""

    def __init__(self, outcome=None) -> None:
        self.records: list[tuple[Material, ExtractionResult]] = []
        self.outcome = outcome

    async def record_extraction(self, material, extraction):
        self.records.append((material, extraction))
        return self.outcome

    async def record_material_extraction(self, material, extraction):
        return type("Outcome", (), {"status": "awaiting_case_binding"})()


class TargetRecordingExtractor:
    """Extractor that records the target_case context it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[Material, object]] = []

    async def extract(self, material: Material) -> ExtractionResult:
        return await self.extract_material(material)

    async def extract_material(self, material, *, corpus_path=None, target_case=None):
        self.calls.append((material, target_case))
        return ExtractionResult(case=target_case)


class LegacyOnlyExtractor:
    """Pre-v0 extractor with only ``extract()`` (no target_case support)."""

    async def extract(self, material: Material) -> ExtractionResult:
        return ExtractionResult(case=target_case())


def make_pipeline(extractor) -> PipelineService:
    return PipelineService(
        indexer=_Indexer(),
        extraction_service=extractor,
        graph_service=_Graph(),
        clock=_Clock(),
        material_resolver=None,
        case_service=_CaseRecorder(
            type(
                "Outcome",
                (),
                {
                    "write": GraphWriteResult((), ("merged-episode",), ()),
                    "material_ids": ("mat-1",),
                },
            )()
        ),
    )


def test_run_material_forwards_the_declared_target_case_to_extractor():
    async def main():
        extractor = TargetRecordingExtractor()
        service = make_pipeline(extractor)
        declared = target_case()

        pipeline_run = await service.run_material(
            make_result(), target_case=declared
        )

        assert pipeline_run.status == "completed"
        assert extractor.calls[0][1] is declared
        assert extractor.calls[0][0].id == "mat-1"
        assert pipeline_run.stages[2].status == "written"

    run(main())


def test_run_material_without_target_keeps_the_legacy_extract_only_path():
    async def main():
        extractor = LegacyOnlyExtractor()
        service = make_pipeline(extractor)

        pipeline_run = await service.run_material(make_result())

        assert pipeline_run.status == "completed"
        assert pipeline_run.stages[1].status == "extracted"

    run(main())


def test_run_material_target_requires_an_evolution_case_object():
    async def main():
        service = make_pipeline(TargetRecordingExtractor())
        with pytest.raises(TypeError) as info:
            await service.run_material(
                make_result(), target_case="case-target-1"  # type: ignore[arg-type]
            )
        assert "EvolutionCase" in str(info.value)

    run(main())


def test_run_material_target_requires_an_extract_material_capable_extractor():
    async def main():
        service = make_pipeline(LegacyOnlyExtractor())
        with pytest.raises(PipelineError) as info:
            await service.run_material(make_result(), target_case=target_case())
        assert info.value.stage == "extract"
        assert "extract_material" in str(info.value)

    run(main())


# ------------------------------------------------------------ case service


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)


class DedupeGraph:
    """Offline graph writer deduplicating episode keys like a real backend."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def add_case(self, case, *, nodes=(), facts=(), claims=(), materials=()):
        self.calls.append((case, tuple(nodes)))
        added = [f"{case.case_id}:episode:{len(self.calls)}"]
        return GraphWriteResult((), tuple(added), ())


def make_case_service(tmp_path: Path) -> tuple[CaseService, CaseExtractionLedger]:
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    service = CaseService(
        ledger=ledger, merger=CaseBundleMerger(), graph_service=DedupeGraph()
    )
    return service, ledger


def bound_extraction(case: EvolutionCase) -> ExtractionResult:
    locator = EvidenceLocator(
        source_id="mat-1",
        corpus_path="corpus/2026-08/example.test/mat-1.md",
        paragraph=1,
        quote="The agency published the revised policy.",
    )
    node = EvolutionNode(
        id="node-1",
        case_id=case.case_id,
        node_type="publication",
        happened_at=PUBLISHED,
        summary="The agency published the revised policy.",
        source_ids=("mat-1",),
        valid_at=PUBLISHED,
        observed_at=PUBLISHED,
        evidence=(locator,),
    )
    fact = TemporalFact(
        subject="Agency",
        predicate="published",
        object="the revised policy",
        valid_at=PUBLISHED,
        invalid_at=None,
        observed_at=PUBLISHED,
        source_ids=("mat-1",),
        confidence=0.9,
        provenance_type="explicit",
        evidence=(locator,),
    )
    claim = Claim(
        claim_id="claim-1",
        actor="Agency",
        proposition="The revised policy matters.",
        stance="support",
        stated_at=PUBLISHED,
        based_on=("mat-1",),
        evidence=(locator,),
        observed_at=PUBLISHED,
    )
    return ExtractionResult(case=case, nodes=(node,), temporal_facts=(fact,), claims=(claim,))


def test_case_service_load_case_returns_the_recorded_case_and_unknown_fails(tmp_path):
    async def main():
        service, ledger = make_case_service(tmp_path)
        try:
            declared = target_case()
            await service.record_extraction(
                make_material(), bound_extraction(declared)
            )

            loaded = service.load_case(TARGET_CASE_ID)
            assert isinstance(loaded, EvolutionCase)
            assert loaded.case_id == TARGET_CASE_ID
            assert loaded.case_type == "policy"
            assert loaded.canonical_name == "Declared target policy"
            assert loaded.start_at == datetime(2026, 1, 1, tzinfo=UTC)
            assert loaded.status == "active"
            # node_ids are derived data; a loaded template never carries them.
            assert loaded.node_ids == ()

            with pytest.raises(LookupError) as info:
                service.load_case("case-unknown")
            assert "case-unknown" in str(info.value)
        finally:
            ledger.close()

    run(main())


# ------------------------------------------------------------- real runtime


DOC_TEMPLATE = """---
source: example.test
title: {title}
published_at: 2026-08-30T09:00:00+00:00
fetched_at: 2026-08-31T12:00:00+00:00
type: policy
case_tags: ["case-1"]
access_level: fulltext
---

{body}
"""


def write_material(home: Path, name: str, body: str) -> Path:
    corpus = home / "corpus" / "2026-08" / "example.test"
    corpus.mkdir(parents=True, exist_ok=True)
    target = corpus / f"{name}.md"
    target.write_text(DOC_TEMPLATE.format(title=name, body=body), encoding="utf-8")
    return target


class FakeGraphBackend:
    def __init__(self) -> None:
        self.episodes: dict[str, GraphEpisode] = {}

    async def add_episode(self, episode: GraphEpisode) -> bool:
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        return tuple(self.episodes.values())


class TargetBoundExtractor:
    """Deterministic extractor honoring the declared target case."""

    name = "target-bound"

    def __init__(self) -> None:
        self.calls: list[tuple[str, EvolutionCase | None]] = []

    async def extract(self, material):
        return await self.extract_material(material)

    async def extract_material(self, material, *, corpus_path=None, target_case=None):
        self.calls.append((material.id, target_case))
        if target_case is None:
            return ExtractionResult()
        locator = EvidenceLocator(
            source_id=material.id,
            corpus_path=f"corpus/2026-08/example.test/{material.id}.md",
            paragraph=1,
            quote=material.content,
        )
        node = EvolutionNode(
            id="node-1",
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
            fact_id="fact-1",
        )
        return ExtractionResult(
            case=target_case,
            nodes=(node,),
            temporal_facts=(fact,),
            claims=(),
        )


def make_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    PrismConfig().save(config_path)
    return config_path


@pytest.fixture(autouse=True)
def isolated_prism_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path))
    return tmp_path


def test_target_case_accumulates_across_materials_and_blocks_cross_case(tmp_path):
    async def main():
        config_path = make_config(tmp_path)
        first_doc = write_material(
            tmp_path, "policy-update", "The agency published the revised policy."
        )
        second_doc = write_material(
            tmp_path, "policy-reaction", "Analysts responded to the revised policy."
        )
        third_doc = write_material(
            tmp_path, "policy-analysis", "Analysts published a separate analysis."
        )
        extractor = TargetBoundExtractor()
        runtime: PrismRuntime = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=extractor,
        )
        try:
            first = await runtime.api.process_material(
                first_doc, target_case=target_case()
            )
            assert first.case_id == TARGET_CASE_ID
            # The extractor received the caller-declared EvolutionCase itself.
            assert extractor.calls[0] == (first.material_id, target_case())
            # The second material names the case by id; the API loads the real
            # recorded EvolutionCase from the durable ledger — never from
            # titles, tags, or vectors.
            second = await runtime.api.process_material(
                second_doc, target_case=TARGET_CASE_ID
            )
            assert second.case_id == TARGET_CASE_ID
            assert extractor.calls[1][0] == second.material_id
            assert extractor.calls[1][1] == target_case()
            assert runtime.case_service.case_for_material(first.material_id) == (
                TARGET_CASE_ID
            )
            assert runtime.case_service.case_for_material(second.material_id) == (
                TARGET_CASE_ID
            )
            outcome = await runtime.case_service.merge_case(TARGET_CASE_ID)
            assert outcome is not None
            assert outcome.material_ids == (first.material_id, second.material_id)
        finally:
            await runtime.close()

        # A fresh process re-processing material 1 under a DIFFERENT recorded
        # target is refused before any write: one material binds one case.
        restarted_extractor = TargetBoundExtractor()
        restarted: PrismRuntime = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=restarted_extractor,
        )
        try:
            other = await restarted.api.process_material(
                third_doc, target_case=target_case(case_id="case-other")
            )
            assert other.case_id == "case-other"
            assert restarted_extractor.calls[0][1] == target_case(case_id="case-other")
            with pytest.raises(MaterialCaseConflict) as info:
                await restarted.api.process_material(
                    first.material_id, target_case="case-other"
                )
            assert info.value.material_id == first.material_id
            assert info.value.case_ids == (TARGET_CASE_ID,)
            assert info.value.attempted_case == "case-other"
            # The refusal happens at the durable binding gate AFTER the
            # extractor honored the second target — never before any write.
            assert restarted_extractor.calls[1][0] == first.material_id
            assert restarted_extractor.calls[1][1] == target_case(case_id="case-other")
            assert restarted.case_service.case_for_material(first.material_id) == (
                TARGET_CASE_ID
            )
            other_entries = restarted.case_ledger.entries("case-other")
            assert len(other_entries) == 1
            assert other_entries[0].material_id == other.material_id
            assert (
                restarted.case_service.case_ids_for_material(first.material_id)
                == (TARGET_CASE_ID,)
            )
            # An unknown declared case id is an explicit error, never a guess.
            with pytest.raises(LookupError) as info:
                await restarted.api.process_material(
                    second.material_id, target_case="case-unknown"
                )
            assert "case-unknown" in str(info.value)
        finally:
            await restarted.close()

    run(main())


# ---------------------------------------------------------------------- CLI


def run_cli(argv, api):
    from prism.cli import main

    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(main(argv, api=api, stdout=stdout, stderr=stderr))
    return status, stdout.getvalue(), stderr.getvalue()


class RecordingAPI:
    def __init__(self) -> None:
        self.processed: list[tuple[object, object, object]] = []

    async def process_material(self, source, metadata=None, target_case=None):
        self.processed.append((source, metadata, target_case))
        case_id = getattr(target_case, "case_id", target_case)
        return {
            "material_id": source,
            "pipeline": {"status": "completed"},
            "case_id": case_id,
            "case_outcome": None,
            "warnings": (),
            "replayed": False,
        }


def test_cli_process_forwards_a_case_id():
    api = RecordingAPI()
    status, out, err = run_cli(["process", "mat-1", "--case-id", TARGET_CASE_ID], api)
    assert status == 0 and err == ""
    assert api.processed == [("mat-1", None, TARGET_CASE_ID)]
    assert json.loads(out)["case_id"] == TARGET_CASE_ID


def test_cli_process_builds_a_real_case_from_case_json():
    api = RecordingAPI()
    payload = json.dumps(
        {
            "case_id": "case-json-1",
            "case_type": "academic_discourse",
            "canonical_name": "JSON declared discourse",
            "start_at": "2025-06-01T00:00:00+00:00",
            "status": "active",
        }
    )
    status, out, err = run_cli(
        ["process", "mat-1", "--case-json", payload], api
    )
    assert status == 0 and err == ""
    source, metadata, target = api.processed[0]
    assert source == "mat-1" and metadata is None
    assert isinstance(target, EvolutionCase)
    assert target.case_id == "case-json-1"
    assert target.case_type == "academic_discourse"
    assert target.canonical_name == "JSON declared discourse"
    assert target.start_at == datetime(2025, 6, 1, tzinfo=UTC)
    assert target.status == "active"
    assert json.loads(out)["case_id"] == "case-json-1"


def test_cli_process_case_flags_are_mutually_exclusive():
    api = RecordingAPI()
    status, out, err = run_cli(
        [
            "process",
            "mat-1",
            "--case-id",
            TARGET_CASE_ID,
            "--case-json",
            '{"case_id": "x"}',
        ],
        api,
    )
    assert status == 2
    assert out == ""
    assert "not allowed with" in err
    assert api.processed == []


def test_cli_process_case_json_missing_identity_is_an_error():
    api = RecordingAPI()
    status, out, err = run_cli(
        ["process", "mat-1", "--case-json", '{"canonical_name": "incomplete"}'],
        api,
    )
    assert status == 1
    assert out == ""
    assert "case_id" in err
    assert api.processed == []
