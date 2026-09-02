"""Offline TDD acceptance tests for Evolution Extraction v0."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prism.analyzer import AnalyzerService
from prism.config import PathConfig
from prism.domain import Claim, Material
from prism.extraction import ExtractionError, ExtractionResult, ExtractionService
from prism.graph import GraphEpisode, GraphService
from prism.ingestion import IngestionResult
from prism.pipeline import PipelineService
from prism.report import ReportService
from prism.store import EvidenceStore


UTC = timezone.utc
PUBLISHED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
BODY = """On 2026-01-10, the ministry proposed the disclosure rule.

On 2026-02-15, the ministry implemented the disclosure rule.

Analysts said the rule may expand next year.

The ministry said adoption increased; researchers said adoption decreased."""


class FakeRouter:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return type("Completion", (), {"text": text})()


class OfflineBackend:
    def __init__(self):
        self.episodes: dict[str, GraphEpisode] = {}

    async def add_episode(self, episode):
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query):
        return tuple(self.episodes.values())


def run(coro):
    return asyncio.run(coro)


def material(**overrides):
    values = dict(
        id="material-evolution",
        title="Disclosure rule chronology",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content=BODY,
        original_format="md",
        case_tags=("case-disclosure",),
    )
    values.update(overrides)
    return Material(**values)


def evidence(quote, paragraph):
    return [
        {
            "source_id": "material-evolution",
            "quote": quote,
            "paragraph": paragraph,
            "page": None,
        }
    ]


def payload():
    return {
        "case": {
            "case_id": "case-disclosure",
            "case_type": "policy",
            "canonical_name": "Disclosure rule",
            "start_at": "2026-01-10T00:00:00+00:00",
            "status": "implemented",
            "node_ids": ["proposal", "implementation"],
            "status_at": "2026-02-15T00:00:00+00:00",
            "status_observed_at": PUBLISHED.isoformat(),
        },
        "nodes": [
            {
                "id": "proposal",
                "case_id": "case-disclosure",
                "node_type": "proposal",
                "assertion_type": "fact",
                "happened_at": "2026-01-10T00:00:00+00:00",
                "valid_at": "2026-01-10T00:00:00+00:00",
                "observed_at": PUBLISHED.isoformat(),
                "summary": "The ministry proposed the rule.",
                "source_ids": ["material-evolution"],
                "claim_ids": [],
                "provenance_type": "source_explicit",
                "evidence": evidence(
                    "On 2026-01-10, the ministry proposed the disclosure rule.", 1
                ),
            },
            {
                "id": "implementation",
                "case_id": "case-disclosure",
                "node_type": "implementation",
                "assertion_type": "fact",
                "happened_at": "2026-02-15T00:00:00+00:00",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "observed_at": PUBLISHED.isoformat(),
                "summary": "The ministry implemented the rule.",
                "source_ids": ["material-evolution"],
                "claim_ids": [],
                "provenance_type": "source_explicit",
                "evidence": evidence(
                    "On 2026-02-15, the ministry implemented the disclosure rule.", 2
                ),
            },
        ],
        "temporal_facts": [
            {
                "subject": "Disclosure rule",
                "predicate": "implementation_status",
                "object": "implemented",
                "assertion_type": "fact",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "invalid_at": None,
                "observed_at": PUBLISHED.isoformat(),
                "source_ids": ["material-evolution"],
                "confidence": 0.97,
                "provenance_type": "source_explicit",
                "evidence": evidence(
                    "On 2026-02-15, the ministry implemented the disclosure rule.", 2
                ),
            }
        ],
        "claims": [
            {
                "claim_id": "forecast",
                "actor": "Analysts",
                "proposition": "The rule may expand next year.",
                "stance": "uncertain",
                "claim_type": "prediction",
                "stated_at": PUBLISHED.isoformat(),
                "observed_at": PUBLISHED.isoformat(),
                "based_on": ["material-evolution"],
                "revised_by": None,
                "provenance_type": "reported",
                "confidence": 0.72,
                "evidence": evidence(
                    "Analysts said the rule may expand next year.", 3
                ),
            }
        ],
        "conflicts": [],
        "warnings": [],
    }


def service(payload_value=None, *, locator=None):
    router = FakeRouter(payload_value if payload_value is not None else payload())
    return ExtractionService(router, evidence_locator=locator), router


def test_extract_material_handles_multiple_events_and_keeps_three_time_axes():
    extractor, router = service()
    result = run(
        extractor.extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert [node.node_type for node in result.nodes] == ["proposal", "implementation"]
    assert result.nodes[0].happened_at < result.nodes[0].observed_at
    assert result.nodes[0].valid_at == result.nodes[0].happened_at
    assert result.nodes[0].observed_at == PUBLISHED
    assert result.nodes[0].evidence[0].corpus_path == "corpus/2026-03/disclosure.md"
    assert result.temporal_facts[0].evidence[0].quote in BODY
    assert router.calls[0][0] == "extract"
    prompt = router.calls[0][1]
    assert "happened_at" in prompt and "valid_at" in prompt and "observed_at" in prompt
    assert "publication" in prompt and "substantive" in prompt


def test_prediction_is_a_claim_not_a_confirmed_fact():
    result = run(
        service()[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert [fact.object for fact in result.temporal_facts] == ["implemented"]
    prediction = result.claims[0]
    assert prediction.claim_type == "prediction"
    assert prediction.stance == "uncertain"
    assert prediction.provenance_type == "reported"
    assert prediction.confidence == 0.72


def test_quote_location_failure_is_an_explicit_gap_and_candidate_never_reaches_output():
    bad = payload()
    bad["nodes"][1]["evidence"][0]["quote"] = "words absent from the material"
    result = run(
        service(bad)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert [node.id for node in result.nodes] == ["proposal"]
    assert result.case.node_ids == ("proposal",)
    assert len(result.evidence_gaps) == 1
    assert result.evidence_gaps[0].item_id == "implementation"
    assert "quote" in result.evidence_gaps[0].detail


def test_no_substantive_change_does_not_fabricate_a_publication_node():
    empty = {
        "case": None,
        "nodes": [],
        "temporal_facts": [],
        "claims": [],
        "conflicts": [],
        "warnings": ["The material only republishes background information."],
    }
    result = run(
        service(empty)[0].extract_material(
            material(content="Background only."),
            corpus_path="corpus/2026-03/background.md",
        )
    )

    assert result.case is None and result.nodes == ()
    assert result.evidence_gaps[0].gap_type == "no_substantive_evolution"


def test_source_conflict_is_preserved_with_verified_evidence():
    conflicted = payload()
    conflicted["conflicts"] = [
        {
            "conflict_id": "adoption-direction",
            "subject": "Rule adoption",
            "predicate": "changed",
            "alternatives": ["increased", "decreased"],
            "source_ids": ["material-evolution"],
            "evidence": evidence(
                "The ministry said adoption increased; researchers said adoption decreased.",
                4,
            ),
        }
    ]
    result = run(
        service(conflicted)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.conflicts[0].alternatives == ("increased", "decreased")
    assert result.conflicts[0].evidence[0].paragraph == 4


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["nodes"][0].update(
            {"happened_at": "2026-03-02T00:00:00+00:00"}
        ),
        lambda value: value["nodes"][0].update({"node_type": "policy_change"}),
        lambda value: value["nodes"][0].update({"source_ids": "material-evolution"}),
        lambda value: value["nodes"][0].update({"source_ids": ["material-other"]}),
        lambda value: value["claims"][0].update({"based_on": []}),
        lambda value: value["claims"][0].update(
            {"evidence": [{"source_id": "material-other", "quote": "Analysts said the rule may expand next year.", "paragraph": 3, "page": None}]}
        ),
        lambda value: value["temporal_facts"][0].update(
            {"assertion_type": "prediction"}
        ),
    ],
)
def test_strict_schema_rejects_future_illegal_missing_source_and_array_errors(mutation):
    invalid = payload()
    mutation(invalid)
    with pytest.raises(ExtractionError):
        run(
            service(invalid)[0].extract_material(
                material(), corpus_path="corpus/2026-03/disclosure.md"
            )
        )


def test_claim_old_positional_constructor_stays_compatible():
    old = Claim(
        "claim-old",
        "Analyst",
        "The policy may help.",
        "uncertain",
        PUBLISHED,
        ("material-evolution",),
        None,
    )
    assert old.provenance_type == "unspecified"
    assert old.confidence == 1.0
    assert old.claim_type == "interpretation"


def _write_corpus(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                'source_id: "material-evolution"',
                'title: "Disclosure rule chronology"',
                'source: "example.test"',
                f'published_at: "{PUBLISHED.isoformat()}"',
                f'fetched_at: "{FETCHED.isoformat()}"',
                'type: "policy"',
                'case_tags: ["case-disclosure"]',
                'original_format: "md"',
                "ocr: false",
                "---",
                "",
                BODY,
                "",
            ]
        ),
        encoding="utf-8",
    )


async def run_pipeline_scenario(tmp_path, *, report_router=None):
    """Run the full offline loop: corpus → index → extract → graph → report.

    Returns the pipeline run, the pre-observation state, the post-observation
    state, the analyzed timeline, and the rendered report (through
    ``report_router`` when supplied, otherwise the deterministic fallback).
    """
    paths = PathConfig(
        data_dir=tmp_path / "data",
        corpus_dir=tmp_path / "corpus",
        raw_dir=tmp_path / "raw",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
    )
    corpus_path = paths.corpus_dir / "2026-03" / "disclosure.md"
    _write_corpus(corpus_path)
    store = EvidenceStore(paths)
    store.initialize()
    backend = OfflineBackend()
    graph = GraphService(backend)
    extractor, _ = service(locator=store.locate)
    pipeline = PipelineService(
        indexer=store,
        extraction_service=extractor,
        graph_service=graph,
    )
    ingested = IngestionResult(
        material(),
        tmp_path / "raw" / "disclosure.md",
        corpus_path,
        False,
        "direct",
    )

    pipeline_result = await pipeline.run_material(ingested)
    analyzer = AnalyzerService(graph)
    before = await analyzer.state(
        "case-disclosure", datetime(2026, 2, 1, tzinfo=UTC)
    )
    after_at = FETCHED + timedelta(days=1)
    after = await analyzer.state("case-disclosure", after_at)
    analysis = await analyzer.analyze("case-disclosure", after_at)
    report = await ReportService(report_router).report(analysis)
    store.close()
    return pipeline_result, before, after, analysis, report


def test_end_to_end_pipeline_graph_cutoff_and_report_counts(tmp_path):
    pipeline_result, before, after, analysis, report = run(
        run_pipeline_scenario(tmp_path)
    )
    extraction = pipeline_result.stages[1].result
    assert extraction.nodes[0].observed_at == PUBLISHED
    assert before.nodes == ()  # later reporting must not leak before observation
    assert len(after.nodes) == 2 and len(after.facts) == 1
    assert after.facts[0].confidence == 0.97
    assert after.interpretations[0].provenance_type == "reported"
    # The prediction claim keeps its layering all the way to the analysis,
    # the report document, and the rendered markdown.
    assert after.interpretations[0].claim_type == "prediction"
    assert report.publication_node_count == 0
    assert report.substantive_node_count == 2
    assert "0 publication node(s)" in report.summary.summary
    assert "2 substantive evolution node(s)" in report.summary.summary
    assert "claim / prediction / uncertain" in report.markdown
    assert all(stage.evidence for stage in (*after.nodes, *after.facts, *after.interpretations))


def test_report_llm_prompt_keeps_claim_type_and_renders_it(tmp_path):
    def summary_payload(analysis):
        claim_stage = next(
            stage for stage in analysis.stages if stage.kind == "claim"
        )
        return {
            "summary": "Analysts predicted the rule may expand.",
            "key_findings": ["Analysts predicted an expansion next year."],
            "turning_points": [],
            "causal_chain": [],
            "uncertainties": ["The expansion forecast remains unconfirmed."],
            "citations": [
                {
                    "episode_keys": [claim_stage.episode_key],
                    "source_ids": ["material-evolution"],
                }
            ],
        }

    _, _, _, analysis, _ = run(run_pipeline_scenario(tmp_path))
    router = FakeRouter(summary_payload(analysis))
    doc = run(ReportService(router).report(analysis))

    assert doc.summary.origin == "llm"
    prompt = router.calls[0][1]
    assert '"claim_type": "prediction"' in prompt
    assert "claim / prediction / uncertain" in doc.markdown


def test_claim_quote_failure_is_an_explicit_gap_and_claim_never_reaches_output():
    bad = payload()
    bad["claims"][0]["evidence"][0]["quote"] = "words that appear nowhere"
    result = run(
        service(bad)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.claims == ()
    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    assert len(result.evidence_gaps) == 1
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "claim"
    assert gap.item_id == "forecast"
    assert "quote" in gap.detail


def test_node_survives_when_one_of_its_claim_quotes_is_unverifiable():
    mixed = payload()
    mixed["nodes"][1]["claim_ids"] = ["forecast", "claim-missing-quote"]
    mixed["claims"].append(
        {
            "claim_id": "claim-missing-quote",
            "actor": "Ministry",
            "proposition": "Adoption increased.",
            "stance": "support",
            "claim_type": "interpretation",
            "stated_at": PUBLISHED.isoformat(),
            "observed_at": PUBLISHED.isoformat(),
            "based_on": ["material-evolution"],
            "revised_by": None,
            "provenance_type": "reported",
            "confidence": 0.9,
            "evidence": evidence("words that appear nowhere", 3),
        }
    )
    result = run(
        service(mixed)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    # The one bad claim becomes a gap; it must not take down the node that
    # referenced it or the claims whose quotes are verifiable.
    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    assert result.case.node_ids == ("proposal", "implementation")
    assert [claim.claim_id for claim in result.claims] == ["forecast"]
    assert [node.claim_ids for node in result.nodes] == [(), ("forecast",)]
    assert result.nodes[1].evidence[0].quote in BODY
    # No dangling references survive into the result.
    accepted = {claim.claim_id for claim in result.claims}
    assert all(
        claim_id in accepted
        for node in result.nodes
        for claim_id in node.claim_ids
    )
    assert len(result.evidence_gaps) == 1
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "claim"
    assert gap.item_id == "claim-missing-quote"
    assert "quote" in gap.detail


def test_node_reference_to_never_proposed_claim_is_still_rejected():
    dangling = payload()
    dangling["nodes"][0]["claim_ids"] = ["claim-nowhere"]

    with pytest.raises(ExtractionError, match="claim_ids"):
        run(
            service(dangling)[0].extract_material(
                material(), corpus_path="corpus/2026-03/disclosure.md"
            )
        )


def test_locator_paragraph_mismatch_keeps_candidate_out_with_gap(tmp_path):
    async def scenario():
        paths = PathConfig(
            data_dir=tmp_path / "data",
            corpus_dir=tmp_path / "corpus",
            raw_dir=tmp_path / "raw",
            cache_dir=tmp_path / "cache",
            output_dir=tmp_path / "output",
        )
        corpus_path = paths.corpus_dir / "2026-03" / "disclosure.md"
        _write_corpus(corpus_path)
        store = EvidenceStore(paths)
        store.initialize()
        store.index_file(corpus_path)
        wrong = payload()
        # The quote lives in paragraph 1; claiming it sits in paragraph 4 is
        # a position error the locator must reject, not silently accept.
        wrong["nodes"][0]["evidence"][0]["paragraph"] = 4
        extractor = ExtractionService(FakeRouter(wrong), evidence_locator=store.locate)
        result = await extractor.extract_material(material(), corpus_path=corpus_path)
        store.close()
        return result

    result = run(scenario())

    assert [node.id for node in result.nodes] == ["implementation"]
    assert result.case.node_ids == ("implementation",)
    assert len(result.evidence_gaps) == 1
    assert result.evidence_gaps[0].item_kind == "node"
    assert result.evidence_gaps[0].item_id == "proposal"
    assert result.temporal_facts[0].evidence[0].paragraph == 2
    assert result.claims[0].evidence[0].paragraph == 3


def test_every_accepted_evidence_locator_is_verbatim_and_corpus_relative():
    result = run(
        service()[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    all_evidence = []
    for node in result.nodes:
        all_evidence.extend(node.evidence)
    for fact in result.temporal_facts:
        all_evidence.extend(fact.evidence)
    for claim in result.claims:
        all_evidence.extend(claim.evidence)
    assert all_evidence
    for locator in all_evidence:
        assert locator.quote in BODY
        assert locator.corpus_path == "corpus/2026-03/disclosure.md"
        assert locator.source_id == "material-evolution"


def test_pipeline_falls_back_to_legacy_extract_method():
    class Indexer:
        def index_file(self, path):
            from prism.store import IndexEntry, IndexOutcome

            return IndexOutcome(
                IndexEntry(
                    "material-evolution",
                    "title",
                    "source",
                    PUBLISHED,
                    FETCHED,
                    "policy",
                    BODY,
                    "corpus/doc.md",
                    "0" * 64,
                ),
                "indexed",
            )

    class LegacyExtractor:
        def __init__(self):
            self.calls = []

        async def extract(self, value):
            self.calls.append(value)
            return ExtractionResult()

    legacy = LegacyExtractor()
    graph = GraphService(OfflineBackend())
    result = IngestionResult(
        material(), Path("raw/doc.md"), Path("corpus/doc.md"), False, "direct"
    )
    pipeline_result = run(
        PipelineService(
            indexer=Indexer(), extraction_service=legacy, graph_service=graph
        ).run_material(result)
    )
    assert legacy.calls == [result.material]
    assert pipeline_result.stages[2].status == "skipped"
