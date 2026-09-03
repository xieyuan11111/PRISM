"""Offline TDD acceptance tests for Evolution Extraction v0."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prism.analyzer import AnalyzerService
from prism.config import PathConfig
from prism.domain import Claim, EvidenceLocator, Material
from prism.extraction import (
    ExtractionError,
    ExtractionEvidenceMatch,
    ExtractionResult,
    ExtractionService,
)
from prism.extraction.textmatch import fold_for_location, resolve_verbatim_spans
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


def evidence(quote, paragraph, page=None):
    return [
        {
            "source_id": "material-evolution",
            "quote": quote,
            "paragraph": paragraph,
            "page": page,
        }
    ]


def small_payload(evidence_entries):
    """A minimal strict payload with one evidence-bound proposal node."""
    return {
        "case": {
            "case_id": "case-disclosure",
            "case_type": "policy",
            "canonical_name": "Disclosure rule",
            "start_at": "2026-01-10T00:00:00+00:00",
            "status": "proposed",
            "node_ids": ["proposal"],
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
                "evidence": evidence_entries,
            }
        ],
        "temporal_facts": [],
        "claims": [],
        "conflicts": [],
        "warnings": [],
    }


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
    prompt = service(conflicted)[0]._prompt(material(), strict=True)
    assert "at least two non-empty strings" in prompt


def test_blank_conflict_alternatives_are_filtered_without_losing_valid_conflict():
    conflicted = payload()
    conflicted["conflicts"] = [
        {
            "conflict_id": "adoption-direction",
            "subject": "Rule adoption",
            "predicate": "changed",
            "alternatives": ["", "increased", "   ", "decreased"],
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

    assert len(result.conflicts) == 1
    assert result.conflicts[0].alternatives == ("increased", "decreased")
    assert result.conflicts[0].source_ids == ("material-evolution",)
    assert result.conflicts[0].evidence[0].paragraph == 4
    assert any(
        "conflicts[0].alternatives" in warning and "filtered" in warning
        for warning in result.warnings
    )


def test_conflict_with_one_valid_alternative_becomes_an_auditable_gap():
    conflicted = payload()
    conflicted["conflicts"] = [
        {
            "conflict_id": "adoption-direction",
            "subject": "Rule adoption",
            "predicate": "changed",
            "alternatives": ["", "increased", "   "],
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

    assert result.conflicts == ()
    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    gaps = [gap for gap in result.evidence_gaps if gap.item_kind == "conflict"]
    assert len(gaps) == 1
    assert gaps[0].gap_type == "insufficient_conflict_alternatives"
    assert gaps[0].item_id == "adoption-direction"
    assert gaps[0].source_ids == ("material-evolution",)
    assert "at least two distinct non-empty alternatives" in gaps[0].detail


@pytest.mark.parametrize(
    "alternatives",
    [
        ["increased"],
        ["increased", "increased"],
        ["increased", None],
    ],
)
def test_conflict_alternative_recovery_does_not_relax_other_shape_rules(
    alternatives,
):
    conflicted = payload()
    conflicted["conflicts"] = [
        {
            "conflict_id": "adoption-direction",
            "subject": "Rule adoption",
            "predicate": "changed",
            "alternatives": alternatives,
            "source_ids": ["material-evolution"],
            "evidence": evidence(
                "The ministry said adoption increased; researchers said adoption decreased.",
                4,
            ),
        }
    ]

    with pytest.raises(ExtractionError, match="alternatives"):
        run(
            service(conflicted)[0].extract_material(
                material(), corpus_path="corpus/2026-03/disclosure.md"
            )
        )


@pytest.mark.parametrize("invalid_boundary", ["source", "evidence"])
def test_conflict_alternative_recovery_still_enforces_evidence_boundaries(
    invalid_boundary,
):
    conflicted = payload()
    candidate = {
        "conflict_id": "adoption-direction",
        "subject": "Rule adoption",
        "predicate": "changed",
        "alternatives": ["", "increased"],
        "source_ids": ["material-evolution"],
        "evidence": evidence(
            "The ministry said adoption increased; researchers said adoption decreased.",
            4,
        ),
    }
    if invalid_boundary == "source":
        candidate["source_ids"] = ["material-other"]
    else:
        candidate["evidence"][0]["quote"] = "words absent from the material"
    conflicted["conflicts"] = [candidate]

    result = run(
        service(conflicted)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.conflicts == ()
    conflict_gaps = [
        gap for gap in result.evidence_gaps if gap.item_kind == "conflict"
    ]
    assert len(conflict_gaps) == 1
    assert conflict_gaps[0].gap_type == "evidence_location_failed"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["nodes"][0].update(
            {"happened_at": "2026-03-02T00:00:00+00:00"}
        ),
        lambda value: value["nodes"][0].update({"node_type": "policy_change"}),
        lambda value: value["nodes"][0].update({"source_ids": "material-evolution"}),
        lambda value: value["claims"][0].update({"based_on": []}),
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


# --- Real-LLM output compatibility (narrow recovery, audited) ---------------
#
# The recoveries below never fabricate evidence: each deviation is answered
# with a safe default, a dropped candidate, or an ignored field, and every
# recovery leaves an explicit warning or evidence gap in the result.


def test_missing_warnings_field_defaults_to_empty_with_audit_notice():
    quiet = payload()
    del quiet["warnings"]
    result = run(
        service(quiet)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    assert result.claims[0].claim_id == "forecast"
    assert len(result.warnings) == 1
    assert "warnings" in result.warnings[0]


def test_scalar_based_on_string_is_normalized_with_audit_notice():
    normalized = payload()
    normalized["claims"][0]["based_on"] = "material-evolution"
    result = run(
        service(normalized)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.claims[0].based_on == ("material-evolution",)
    assert result.claims[0].evidence[0].quote in BODY
    assert any(
        "based_on" in warning and "normalized" in warning
        for warning in result.warnings
    )


def test_scalar_based_on_referencing_other_material_drops_only_that_claim():
    foreign = payload()
    foreign["claims"][0]["based_on"] = "material-other"
    result = run(
        service(foreign)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.claims == ()
    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "claim" and gap.item_id == "forecast"
    assert "may reference only input material" in gap.detail


def test_foreign_based_on_reference_drops_only_that_claim():
    foreign = payload()
    foreign["claims"][0]["based_on"] = ["material-other"]
    result = run(
        service(foreign)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.claims == ()
    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    assert result.temporal_facts[0].object == "implemented"
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "claim" and gap.item_id == "forecast"
    assert "may reference only input material" in gap.detail


def test_foreign_evidence_source_id_drops_only_that_claim():
    foreign = payload()
    foreign["claims"][0]["evidence"] = [
        {
            "source_id": "material-other",
            "quote": "Analysts said the rule may expand next year.",
            "paragraph": 3,
            "page": None,
        }
    ]
    result = run(
        service(foreign)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.claims == ()
    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "claim" and gap.item_id == "forecast"
    assert "material-other" in gap.detail


def test_foreign_node_source_reference_drops_only_that_node():
    foreign = payload()
    foreign["nodes"][0]["source_ids"] = ["material-other"]
    result = run(
        service(foreign)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert [node.id for node in result.nodes] == ["implementation"]
    assert result.case.node_ids == ("implementation",)
    assert result.claims[0].claim_id == "forecast"
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "node" and gap.item_id == "proposal"
    assert "may reference only input material" in gap.detail


@pytest.mark.parametrize("case_id", ["", None])
def test_unusable_case_id_degrades_to_no_case_with_candidate_gaps(case_id):
    degraded = payload()
    degraded["case"]["case_id"] = case_id
    result = run(
        service(degraded)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.case is None
    assert result.nodes == () and result.temporal_facts == ()
    assert result.claims == () and result.conflicts == ()
    kinds = {(gap.gap_type, gap.item_kind) for gap in result.evidence_gaps}
    assert ("unusable_case", "node") in kinds
    assert ("unusable_case", "temporal_fact") in kinds
    assert ("unusable_case", "claim") in kinds
    case_gaps = [gap for gap in result.evidence_gaps if gap.item_kind is None]
    assert len(case_gaps) == 1
    assert "case_id" in case_gaps[0].detail
    # The dropped candidate ids stay auditable, and the result never claims
    # the material lacked substantive evolution when it merely lacked a case.
    assert {"proposal", "implementation", "forecast"} <= {
        gap.item_id for gap in result.evidence_gaps
    }
    assert all(
        gap.gap_type != "no_substantive_evolution" for gap in result.evidence_gaps
    )


def test_unusable_case_id_with_no_candidates_records_single_gap():
    degraded = payload()
    degraded["case"]["case_id"] = ""
    degraded.update(nodes=[], temporal_facts=[], claims=[], conflicts=[])
    result = run(
        service(degraded)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.case is None
    assert len(result.evidence_gaps) == 1
    assert result.evidence_gaps[0].gap_type == "unusable_case"


def test_case_null_with_candidates_is_still_rejected():
    contradictory = payload()
    contradictory["case"] = None
    with pytest.raises(ExtractionError, match="case must be non-null"):
        run(
            service(contradictory)[0].extract_material(
                material(), corpus_path="corpus/2026-03/disclosure.md"
            )
        )


@pytest.mark.parametrize(
    "hoisted",
    [
        [
            {
                "source_id": "material-evolution",
                "quote": "On 2026-01-10, the ministry proposed the disclosure rule.",
                "paragraph": 1,
                "page": None,
            }
        ],
        "material-evolution paragraph 1",
    ],
)
def test_top_level_evidence_field_is_ignored_never_bound(hoisted):
    payload_value = payload()
    payload_value["evidence"] = hoisted
    result = run(
        service(payload_value)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    assert result.claims[0].claim_id == "forecast"
    assert len(result.warnings) == 1
    assert "top-level evidence" in result.warnings[0]
    # Accepted locators still come only from per-candidate evidence fields.
    assert result.nodes[0].evidence[0].paragraph == 1
    assert all(
        locator.quote in BODY
        for node in result.nodes
        for locator in node.evidence
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"warnings": {"message": "not an array"}}),
        lambda value: value["claims"][0].update({"based_on": ""}),
        lambda value: value.update({"api_key": "untrusted"}),
    ],
)
def test_compat_recovery_never_weakens_remaining_strict_validation(mutation):
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


# Migration note (2026-09-03): a claimed paragraph that does not contain the
# quote used to drop the candidate outright.  The paragraph contract stays
# "one-based non-empty lines", but a quote that occurs in exactly one
# paragraph of the material is now safely relocated to that paragraph with an
# explicit warning and an auditable match record; only ambiguous or absent
# quotes remain gaps (see the offline recovery tests below).


def test_seam_paragraph_mismatch_with_unique_quote_recovers_with_warning(tmp_path):
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
        # a numbering error, but the quote occurs in exactly one paragraph,
        # so the locator re-anchors it to paragraph 1 and records the event.
        wrong["nodes"][0]["evidence"][0]["paragraph"] = 4
        extractor = ExtractionService(FakeRouter(wrong), evidence_locator=store.locate)
        result = await extractor.extract_material(material(), corpus_path=corpus_path)
        store.close()
        return result

    result = run(scenario())

    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    assert result.nodes[0].evidence[0].paragraph == 1
    assert result.temporal_facts[0].evidence[0].paragraph == 2
    assert result.claims[0].evidence[0].paragraph == 3
    assert any(
        "paragraph 4" in warning and "paragraph 1" in warning
        for warning in result.warnings
    )
    recovered = [m for m in result.evidence_matches if m.paragraph_recovered]
    assert len(recovered) == 1
    assert recovered[0].path == "nodes[0].evidence[0]"
    assert recovered[0].paragraph == 1
    assert recovered[0].requested_paragraph == 4


# --- Evidence binding tolerance and audit -----------------------------------
#
# Real models copy quotes almost verbatim: collapsed double spaces, NBSP
# swapped for spaces, straight quotes for curly ones.  Locating may fold these
# safe differences, but the stored locator quote must stay a continuous,
# character-exact slice of the material, and every deviation must be
# auditable.  Paraphrases never bind.


DOUBLE_SPACE_BODY = (
    "On 2026-01-10, the ministry proposed the disclosure rule.\n"
    "\n"
    "The ministry published the  revised policy today.\n"
    "\n"
    "Analysts reacted quickly.\n"
)

CURLY_BODY = (
    "On 2026-01-10, the ministry proposed the disclosure rule.\n"
    "\n"
    "The ministry called it a \u201cmajor step\u201d for transparency.\n"
    "\n"
    "Analysts reacted quickly.\n"
)


def run_small(payload_value, content):
    return run(
        service(payload_value)[0].extract_material(
            material(content=content), corpus_path="corpus/2026-03/policy.md"
        )
    )


def test_whitespace_variant_quote_binds_to_verbatim_source_text_with_audit():
    variant = small_payload(
        evidence("The ministry published the revised policy today.", 2)
    )

    result = run_small(variant, DOUBLE_SPACE_BODY)

    assert [node.id for node in result.nodes] == ["proposal"]
    locator = result.nodes[0].evidence[0]
    assert locator.paragraph == 2
    assert locator.quote == "The ministry published the  revised policy today."
    assert locator.quote in DOUBLE_SPACE_BODY
    match = result.evidence_matches[0]
    assert match.path == "nodes[0].evidence[0]"
    assert match.match_type == "whitespace_normalized"
    assert match.paragraph == 2
    assert match.requested_paragraph == 2
    assert match.paragraph_recovered is False
    assert any(
        "whitespace" in warning.lower() and "normaliz" in warning.lower()
        for warning in result.warnings
    )


def test_unicode_punctuation_variant_quote_binds_to_verbatim_source_text():
    variant = small_payload(
        evidence('The ministry called it a "major step" for transparency.', 2)
    )

    result = run_small(variant, CURLY_BODY)

    assert [node.id for node in result.nodes] == ["proposal"]
    locator = result.nodes[0].evidence[0]
    assert locator.quote == (
        "The ministry called it a \u201cmajor step\u201d for transparency."
    )
    assert "\u201c" in locator.quote and "\u201d" in locator.quote
    assert locator.quote in CURLY_BODY
    assert result.evidence_matches[0].match_type == "whitespace_normalized"


def test_exact_quotes_record_exact_matches_and_pass_pdf_page_through():
    exact = small_payload(
        evidence(
            "On 2026-01-10, the ministry proposed the disclosure rule.", 1, page=3
        )
    )

    result = run_small(exact, DOUBLE_SPACE_BODY)

    assert result.nodes[0].evidence[0].page == 3
    assert result.evidence_matches[0].match_type == "exact"
    assert result.evidence_matches[0].paragraph == 1
    assert result.evidence_matches[0].requested_paragraph == 1
    assert result.evidence_matches[0].paragraph_recovered is False


def test_pdf_page_passes_through_the_injected_locator_seam():
    calls = []

    def locator(source_id, *, quote, paragraph, page):
        calls.append((source_id, quote, paragraph, page))
        return EvidenceLocator(
            source_id=source_id,
            corpus_path="corpus/2026-03/policy.pdf",
            paragraph=paragraph,
            page=page,
            quote=quote,
        )

    exact_quote = "On 2026-01-10, the ministry proposed the disclosure rule."
    pdf = small_payload(evidence(exact_quote, 1, page=3))

    result = run(
        service(pdf, locator=locator)[0].extract_material(
            material(content=DOUBLE_SPACE_BODY),
            corpus_path="corpus/2026-03/policy.pdf",
        )
    )

    assert calls == [("material-evolution", exact_quote, 1, 3)]
    assert result.nodes[0].evidence[0].page == 3


def test_match_records_cover_every_bound_locator_for_audit():
    result = run(
        service()[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert {m.path: m.match_type for m in result.evidence_matches} == {
        "nodes[0].evidence[0]": "exact",
        "nodes[1].evidence[0]": "exact",
        "temporal_facts[0].evidence[0]": "exact",
        "claims[0].evidence[0]": "exact",
    }
    assert all(
        m.paragraph_recovered is False and m.source_id == "material-evolution"
        for m in result.evidence_matches
    )


def test_wrong_paragraph_with_unique_quote_recovers_to_real_paragraph():
    wrong = small_payload(
        evidence("On 2026-01-10, the ministry proposed the disclosure rule.", 4)
    )

    result = run_small(wrong, DOUBLE_SPACE_BODY)

    assert [node.id for node in result.nodes] == ["proposal"]
    assert result.nodes[0].evidence[0].paragraph == 1
    match = result.evidence_matches[0]
    assert match.match_type == "exact"
    assert match.paragraph_recovered is True
    assert match.requested_paragraph == 4
    assert match.paragraph == 1
    assert any(
        "paragraph 4" in warning and "paragraph 1" in warning
        for warning in result.warnings
    )


def test_wrong_paragraph_with_ambiguous_quote_stays_a_gap():
    ambiguous_body = (
        "The rule expands coverage.\n"
        "\n"
        "The rule expands costs, critics said.\n"
        "\n"
        "Analysts reacted quickly.\n"
    )
    wrong = small_payload(evidence("The rule expands", 3))

    result = run_small(wrong, ambiguous_body)

    assert result.nodes == ()
    assert result.case.node_ids == ()
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "node" and gap.item_id == "proposal"
    assert "paragraph" in gap.detail and "1" in gap.detail and "2" in gap.detail
    assert result.evidence_matches == ()


def test_quote_spanning_two_paragraphs_is_rejected_as_a_gap():
    wrapped_body = (
        "The ministry proposed\nthe disclosure rule.\n"
        "\n"
        "Analysts reacted quickly.\n"
    )
    joined = small_payload(evidence("The ministry proposed the disclosure rule.", 1))

    result = run_small(joined, wrapped_body)

    assert result.nodes == ()
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "node"
    assert "single non-empty paragraph" in gap.detail
    assert result.evidence_matches == ()


def test_reworded_quote_is_never_accepted_as_evidence():
    reworded = small_payload(
        evidence("The ministry today published a revised policy.", 2)
    )

    result = run_small(reworded, DOUBLE_SPACE_BODY)

    assert result.nodes == ()
    gap = result.evidence_gaps[0]
    assert gap.gap_type == "evidence_location_failed"
    assert "quote" in gap.detail
    assert result.evidence_matches == ()


# --- Locator folding safety ---------------------------------------------------
#
# Security hardening (2026-09-03 review): locating may fold only whitespace
# runs (to a single boundary space, never deletion) and a closed set of
# single-character punctuation lookalikes.  No general Unicode
# normalization, so circled digits, full-width letters and multi-character
# expansions can never widen recall; a quote that only matches through such
# a generalization degrades to an evidence gap instead.


def test_fold_collapses_whitespace_runs_but_never_fuses_words():
    assert fold_for_location("not  able") == fold_for_location("not able")
    assert fold_for_location("not able") != fold_for_location("notable")
    assert fold_for_location("a\u00a0b") == fold_for_location("a b")
    assert fold_for_location(" padded\t") == fold_for_location("padded")


def test_fold_generalizes_no_unicode_lookalike_classes():
    assert fold_for_location("\u2460") != fold_for_location("1")
    assert fold_for_location("\uff23") != fold_for_location("C")
    assert fold_for_location("\u2026") != fold_for_location("...")
    assert fold_for_location("\u2018x\u2019") == fold_for_location("'x'")
    assert fold_for_location("\u2013dash") == fold_for_location("-dash")


def test_resolved_spans_stay_continuous_slices_with_equal_folds():
    content = "The ministry published the  revised policy today."

    spans = resolve_verbatim_spans(content, "published the revised policy")

    assert len(spans) == 1
    start, end = spans[0]
    assert content[start:end] == "published the  revised policy"
    assert fold_for_location(content[start:end]) == fold_for_location(
        "published the revised policy"
    )


def test_single_dot_quote_never_resolves_to_an_ellipsis_character():
    content = "The review continues\u2026 elsewhere"

    assert resolve_verbatim_spans(content, ".") == ()
    assert resolve_verbatim_spans(content, "continues. elsewhere") == ()


def test_quote_with_a_space_never_resolves_across_fused_words():
    assert resolve_verbatim_spans(
        "called the draft notable today", "not able"
    ) == ()


FUSED_BODY = (
    "On 2026-01-10, the ministry proposed the disclosure rule.\n"
    "\n"
    "The reviewer called the draft notable for its clarity.\n"
    "\n"
    "Analysts reacted quickly.\n"
)


def test_whitespace_deletion_never_fuses_words_into_false_evidence():
    fused = small_payload(evidence("not able for its clarity", 2))

    result = run_small(fused, FUSED_BODY)

    assert result.nodes == ()
    gap = result.evidence_gaps[0]
    assert gap.gap_type == "evidence_location_failed"
    assert result.evidence_matches == ()


LOOKALIKE_BODY = (
    "The rule reached step \u2460 of the plan.\n"
    "\n"
    "The board graded the draft \uff23-minus.\n"
    "\n"
    "Analysts reacted quickly.\n"
)


@pytest.mark.parametrize(
    ("quote", "paragraph"),
    [
        ("The rule reached step 1 of the plan.", 1),
        ("The board graded the draft C-minus.", 2),
    ],
)
def test_unicode_lookalike_classes_never_become_matching_evidence(quote, paragraph):
    lookalike = small_payload(evidence(quote, paragraph))

    result = run_small(lookalike, LOOKALIKE_BODY)

    assert result.nodes == ()
    assert result.evidence_matches == ()


ELLIPSIS_BODY = (
    "The ministry said the review continues\u2026 elsewhere\n"
)


def test_ellipsis_character_never_binds_an_expanded_dot_quote():
    dotted = small_payload(evidence("continues... elsewhere", 1))

    result = run_small(dotted, ELLIPSIS_BODY)

    assert result.nodes == ()
    assert result.evidence_matches == ()


def test_ellipsis_character_never_binds_a_single_dot_quote():
    dotted = small_payload(evidence(".", 1))

    result = run_small(dotted, ELLIPSIS_BODY)

    assert result.nodes == ()
    assert result.evidence_matches == ()


NORMALIZED_TWIN_BODY = (
    "Today the ministry published the revised policy.\n"
    "\n"
    "Later the ministry published the  revised policy again.\n"
    "\n"
    "Analysts reacted quickly.\n"
)


def test_exact_classification_follows_the_selected_span_not_any_occurrence():
    variant = small_payload(
        evidence("the ministry published the revised policy", 2)
    )

    result = run_small(variant, NORMALIZED_TWIN_BODY)

    assert [node.id for node in result.nodes] == ["proposal"]
    assert result.nodes[0].evidence[0].paragraph == 2
    assert result.nodes[0].evidence[0].quote == (
        "the ministry published the  revised policy"
    )
    assert result.evidence_matches[0].match_type == "whitespace_normalized"


def test_same_paragraph_prefers_the_truly_exact_span_before_normalized_ones():
    body = (
        "the ministry published the  revised policy first, then the ministry "
        "published the revised policy again.\n"
        "\n"
        "Analysts reacted quickly.\n"
    )
    variant = small_payload(
        evidence("the ministry published the revised policy", 1)
    )

    result = run_small(variant, body)

    assert result.nodes[0].evidence[0].quote == (
        "the ministry published the revised policy"
    )
    assert result.evidence_matches[0].match_type == "exact"


def test_failed_second_locator_leaves_no_residue_in_evidence_matches():
    partial = small_payload(
        [
            {
                "source_id": "material-evolution",
                "quote": "On 2026-01-10, the ministry proposed the disclosure rule.",
                "paragraph": 1,
                "page": None,
            },
            {
                "source_id": "material-evolution",
                "quote": "words absent from the material",
                "paragraph": 2,
                "page": None,
            },
        ]
    )

    result = run_small(partial, DOUBLE_SPACE_BODY)

    assert result.nodes == ()
    gap = result.evidence_gaps[0]
    assert gap.item_kind == "node" and gap.item_id == "proposal"
    assert result.evidence_matches == ()


def test_conflict_rejected_for_alternatives_leaves_no_match_residue():
    conflicted = payload()
    conflicted["conflicts"] = [
        {
            "conflict_id": "adoption-direction",
            "subject": "Rule adoption",
            "predicate": "changed",
            "alternatives": ["", "increased", "   "],
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

    assert result.conflicts == ()
    assert not any(
        match.path.startswith("conflicts[0]") for match in result.evidence_matches
    )
    assert len(result.evidence_matches) == 4


def test_unusable_case_exclusions_leave_no_match_residue():
    degraded = payload()
    degraded["case"]["case_id"] = ""

    result = run(
        service(degraded)[0].extract_material(
            material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.nodes == () and result.conflicts == ()
    assert result.evidence_matches == ()


def test_recovery_warning_claims_one_paragraph_not_one_occurrence():
    wrong = small_payload(
        evidence("On 2026-01-10, the ministry proposed the disclosure rule.", 4)
    )

    result = run_small(wrong, DOUBLE_SPACE_BODY)

    assert any(
        "in exactly one paragraph" in warning for warning in result.warnings
    )


def test_prompt_demands_verbatim_quotes_and_line_based_paragraph_numbers():
    prompt = service()[0]._prompt(material(), strict=True)

    assert "verbatim, character for character" in prompt
    assert "never retype, normalize, translate or paraphrase" in prompt
    assert "count only non-empty lines in order and skip blank lines" in prompt
    assert "do not emit that candidate at all" in prompt


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
