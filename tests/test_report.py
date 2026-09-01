import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from prism.analyzer import (
    ChangeReason,
    EvidenceGap,
    EvolutionAnalysis,
    OpenQuestion,
    TimelineStage,
    TurningPoint,
)
from prism.llm import Completion, RetriesExhaustedError
from prism.report import (
    SUMMARY_ORIGIN_FALLBACK,
    SUMMARY_ORIGIN_LLM,
    ReportCitation,
    ReportDocument,
    ReportService,
    ReportSummary,
)


UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
CASE_ID = "housing_policy_2026"
CASE_EPISODE = "case-housing-2026"
NODE_EPISODE = "node-publication"
FACT_EPISODE = "fact-loan-terms"
CLAIM_EPISODE = "claim-market-impact"
SECRET = "super-secret-provider-key"


def make_analysis(**overrides):
    values = {
        "case_id": CASE_ID,
        "as_of": AS_OF,
        "case_type": "policy",
        "stages": (
            TimelineStage(
                episode_key=CASE_EPISODE,
                kind="evolution_case",
                layer="fact",
                summary="Case housing_policy_2026 tracks the 2026 housing policy.",
                valid_at=datetime(2026, 8, 1, tzinfo=UTC),
                invalid_at=None,
                reference_time=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
                source_ids=("material-case",),
            ),
            TimelineStage(
                episode_key=NODE_EPISODE,
                kind="evolution_node",
                layer="fact",
                summary="The revised housing policy was published.",
                valid_at=datetime(2026, 8, 31, 9, 30, tzinfo=UTC),
                invalid_at=None,
                reference_time=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
                source_ids=("material-1", "material-2"),
                node_type="publication",
                confidence=0.9,
                provenance_type="explicit",
            ),
            TimelineStage(
                episode_key=FACT_EPISODE,
                kind="temporal_fact",
                layer="fact",
                summary="Loan terms allowed 30-year repayment.",
                valid_at=datetime(2026, 8, 20, tzinfo=UTC),
                invalid_at=datetime(2026, 8, 31, 9, 30, tzinfo=UTC),
                reference_time=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
                source_ids=("material-1",),
                confidence=0.8,
                provenance_type="explicit",
            ),
            TimelineStage(
                episode_key=CLAIM_EPISODE,
                kind="claim",
                layer="interpretation",
                summary="The agency expects the revision to improve market liquidity.",
                valid_at=datetime(2026, 8, 31, 11, 0, tzinfo=UTC),
                invalid_at=None,
                reference_time=datetime(2026, 8, 31, 11, 0, tzinfo=UTC),
                source_ids=("material-3",),
                stance="uncertain",
            ),
        ),
        "turning_points": (
            TurningPoint(
                NODE_EPISODE,
                "publication",
                datetime(2026, 8, 31, 9, 30, tzinfo=UTC),
                "The revised housing policy was published.",
                ("material-1", "material-2"),
            ),
            TurningPoint(
                FACT_EPISODE,
                "fact_superseded",
                datetime(2026, 8, 31, 9, 30, tzinfo=UTC),
                "Loan terms allowed 30-year repayment.",
                ("material-1",),
            ),
        ),
        "change_reasons": (
            ChangeReason(
                FACT_EPISODE,
                "fact_superseded",
                "fact_change",
                datetime(2026, 8, 31, 9, 30, tzinfo=UTC),
                "Loan terms allowed 30-year repayment. ceased to be valid.",
                ("material-1",),
            ),
        ),
        "evidence_gaps": (
            EvidenceGap(
                "unattributed_entry",
                "temporal_fact entry 'fact-execution-rate' has no source_ids",
                "fact-execution-rate",
            ),
        ),
        "open_questions": (
            OpenQuestion(
                CLAIM_EPISODE,
                "uncertain_claim",
                "Will the revision improve market liquidity?",
                "Agency",
                datetime(2026, 8, 31, 11, 0, tzinfo=UTC),
                ("material-3",),
            ),
        ),
    }
    values.update(overrides)
    return EvolutionAnalysis(**values)


def llm_payload():
    return {
        "summary": "The case moved from proposal to publication within August 2026.",
        "key_findings": ["The revised policy was published on 2026-08-31."],
        "turning_points": ["Publication of the revised policy."],
        "causal_chain": ["The earlier fact ceased to be valid at publication."],
        "uncertainties": ["The agency's stated rationale remains uncertain."],
        "citations": [
            {
                "episode_keys": [NODE_EPISODE],
                "source_ids": ["material-1", "material-2"],
            },
            {"episode_keys": [FACT_EPISODE], "source_ids": ["material-1"]},
        ],
    }


class FakeRouter:
    def __init__(self, payload, *, error=None, secret=SECRET):
        self.payload = payload
        self.error = error
        self.secret = secret
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        if self.error is not None:
            raise self.error
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return Completion(text=text, provider=self.secret, model=self.secret)


def run_report(analysis, router=None):
    return asyncio.run(ReportService(router).report(analysis))


def bad_payloads():
    payload = llm_payload()
    del payload["citations"]
    return [
        pytest.param("not json at all", id="not-json"),
        pytest.param(payload, id="missing-citations"),
        pytest.param({**llm_payload(), "bonus": 1}, id="unexpected-field"),
        pytest.param({**llm_payload(), "summary": ""}, id="empty-summary"),
        pytest.param(
            {
                **llm_payload(),
                "key_findings": ["ok", 1],
            },
            id="findings-not-strings",
        ),
        pytest.param(
            {
                **llm_payload(),
                "citations": [
                    {"episode_keys": ["ghost-episode"], "source_ids": ["material-1"]}
                ],
            },
            id="unknown-episode-key",
        ),
        pytest.param(
            {
                **llm_payload(),
                "citations": [
                    {"episode_keys": [NODE_EPISODE], "source_ids": ["ghost-source"]}
                ],
            },
            id="unknown-source-id",
        ),
        pytest.param(
            {
                **llm_payload(),
                "citations": [{"episode_keys": [NODE_EPISODE], "source_ids": []}],
            },
            id="citation-without-sources",
        ),
        pytest.param(
            {
                **llm_payload(),
                "citations": [{"source_ids": ["material-1"]}],
            },
            id="citation-missing-episode-keys",
        ),
        pytest.param({**llm_payload(), "citations": []}, id="no-citations"),
    ]


def test_report_without_router_is_deterministic_and_complete():
    analysis = make_analysis()
    doc = run_report(analysis)

    assert isinstance(doc, ReportDocument)
    assert doc.case_id == CASE_ID
    assert doc.as_of == AS_OF
    assert doc.case_type == "policy"
    assert doc.summary.origin == SUMMARY_ORIGIN_FALLBACK
    assert doc.stages == analysis.stages
    assert doc.turning_points == analysis.turning_points
    assert doc.change_reasons == analysis.change_reasons
    assert doc.evidence_gaps == analysis.evidence_gaps
    assert doc.open_questions == analysis.open_questions

    markdown = doc.markdown
    for expected in (
        f"# Evolution Report: {CASE_ID}",
        "As of:",
        "## Timeline Stages",
        "## Turning Points",
        "## Change Reasons",
        "## Evidence Gaps",
        "## Open Questions",
        "## Citations",
        CASE_EPISODE,
        NODE_EPISODE,
        FACT_EPISODE,
        CLAIM_EPISODE,
        "The revised housing policy was published.",
        "Loan terms allowed 30-year repayment.",
        "publication",
        "fact_superseded",
        "Will the revision improve market liquidity?",
        "material-1",
        "material-2",
        "material-3",
        "material-case",
        "deterministic fallback",
    ):
        assert expected in markdown

    again = run_report(make_analysis())
    assert again == doc
    assert again.markdown == markdown


def test_document_citations_cover_every_referenced_source_sorted():
    doc = run_report(make_analysis())

    assert [citation.source_id for citation in doc.citations] == [
        "material-1",
        "material-2",
        "material-3",
        "material-case",
    ]
    by_id = {citation.source_id: citation.episode_keys for citation in doc.citations}
    assert set(by_id["material-1"]) == {NODE_EPISODE, FACT_EPISODE}
    assert by_id["material-2"] == (NODE_EPISODE,)
    assert by_id["material-3"] == (CLAIM_EPISODE,)
    assert by_id["material-case"] == (CASE_EPISODE,)


def test_fallback_never_fabricates_causality():
    doc = run_report(make_analysis(change_reasons=()))

    assert doc.summary.causal_chain == ()
    assert doc.summary.origin == SUMMARY_ORIGIN_FALLBACK
    assert "No recorded change reasons; no causal chain is asserted." in doc.markdown
    assert "None recorded in the available evidence." in doc.markdown


def test_contracts_are_frozen_slots_and_validated():
    doc = run_report(make_analysis())

    with pytest.raises(FrozenInstanceError):
        doc.case_id = "x"
    with pytest.raises(FrozenInstanceError):
        doc.summary.summary = "x"
    with pytest.raises(FrozenInstanceError):
        doc.citations[0].source_id = "x"
    assert not hasattr(doc, "__dict__")
    assert not hasattr(doc.summary, "__dict__")
    assert not hasattr(doc.citations[0], "__dict__")

    with pytest.raises(ValueError):
        ReportSummary(summary="s", origin="magic")
    with pytest.raises(ValueError):
        ReportCitation(source_id=" ")
    with pytest.raises(TypeError):
        ReportSummary(summary="s", citations=("nope",))

    base = dict(
        case_id=CASE_ID,
        as_of=AS_OF,
        case_type="policy",
        summary=ReportSummary(summary="s"),
        stages=(),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
        citations=(),
        markdown="# report",
    )
    with pytest.raises(ValueError):
        ReportDocument(**{**base, "as_of": datetime(2026, 9, 1)})
    with pytest.raises(TypeError):
        ReportDocument(**{**base, "stages": ("nope",)})
    with pytest.raises(ValueError):
        ReportDocument(**{**base, "markdown": " "})


def test_router_happy_path_uses_summarize_report_role_and_analysis_only_prompt():
    router = FakeRouter(llm_payload())
    doc = run_report(make_analysis(), router)

    assert [call[0] for call in router.calls] == ["summarize_report"]
    prompt = router.calls[0][1]
    for token in (
        CASE_ID,
        CASE_EPISODE,
        NODE_EPISODE,
        FACT_EPISODE,
        CLAIM_EPISODE,
        "material-1",
        "material-2",
        "material-3",
        "BEGIN ANALYSIS",
        "END ANALYSIS",
    ):
        assert token in prompt
    assert SECRET not in prompt

    assert doc.summary.origin == SUMMARY_ORIGIN_LLM
    assert doc.summary.summary == "The case moved from proposal to publication within August 2026."
    assert doc.summary.key_findings == ("The revised policy was published on 2026-08-31.",)
    assert doc.summary.turning_points == ("Publication of the revised policy.",)
    assert doc.summary.uncertainties == ("The agency's stated rationale remains uncertain.",)
    assert [citation.source_id for citation in doc.summary.citations] == [
        "material-1",
        "material-2",
    ]
    summary_citation = doc.summary.citations[0]
    assert summary_citation.episode_keys == (FACT_EPISODE, NODE_EPISODE)

    assert "model-distilled" in doc.markdown
    assert "deterministic fallback" not in doc.markdown
    assert "The case moved from proposal to publication within August 2026." in doc.markdown


def test_fenced_json_completion_is_accepted():
    text = "```json\n" + json.dumps(llm_payload()) + "\n```"
    doc = run_report(make_analysis(), FakeRouter(text))

    assert doc.summary.origin == SUMMARY_ORIGIN_LLM


def test_llm_summary_cannot_override_structured_facts():
    payload = llm_payload()
    payload["key_findings"] = ["The policy was repealed and all loans were cancelled."]
    analysis = make_analysis()
    doc = run_report(analysis, FakeRouter(payload))

    assert doc.stages == analysis.stages
    assert doc.turning_points == analysis.turning_points
    assert doc.change_reasons == analysis.change_reasons
    assert "The revised housing policy was published." in doc.markdown
    structured_body = doc.markdown.split("## Timeline Stages", 1)[1]
    assert "The policy was repealed and all loans were cancelled." not in structured_body


@pytest.mark.parametrize("payload", bad_payloads())
def test_invalid_llm_payloads_fall_back_deterministically(payload):
    router = FakeRouter(payload)
    doc = run_report(make_analysis(), router)

    assert len(router.calls) == 1
    assert doc.summary.origin == SUMMARY_ORIGIN_FALLBACK
    assert "## Timeline Stages" in doc.markdown
    assert "## Citations" in doc.markdown
    assert "The revised housing policy was published." in doc.markdown


def test_router_failure_falls_back_without_fabrication():
    for error in (
        RetriesExhaustedError(("provider-a",), 2),
        RuntimeError("transport exploded"),
    ):
        router = FakeRouter(llm_payload(), error=error)
        doc = run_report(make_analysis(), router)

        assert doc.summary.origin == SUMMARY_ORIGIN_FALLBACK
        assert "## Timeline Stages" in doc.markdown
        assert "## Citations" in doc.markdown


def test_secrets_never_enter_prompt_or_report():
    secret = "sk-live-secret-value"
    router = FakeRouter(llm_payload(), secret=secret)
    doc = run_report(make_analysis(), router)

    assert secret not in router.calls[0][1]
    assert secret not in doc.markdown
    assert secret not in repr(doc)
    assert secret not in repr(doc.summary)


def test_empty_analysis_reports_gap_and_falls_back():
    empty = EvolutionAnalysis(
        case_id=CASE_ID,
        as_of=AS_OF,
        case_type=None,
        stages=(),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(
            EvidenceGap("empty_timeline", f"no timeline entries for case {CASE_ID!r}"),
        ),
        open_questions=(),
    )

    doc = run_report(empty, FakeRouter(llm_payload()))
    assert doc.summary.origin == SUMMARY_ORIGIN_FALLBACK
    assert doc.citations == ()
    assert "no timeline entries" in doc.markdown

    bare = run_report(empty)
    assert bare.summary.origin == SUMMARY_ORIGIN_FALLBACK
    assert "The recorded timeline is empty." in bare.markdown


def test_service_rejects_invalid_dependencies_and_inputs():
    ReportService()
    with pytest.raises(TypeError):
        ReportService(router="nope")
    with pytest.raises(TypeError):
        run_report("not-an-analysis")
    with pytest.raises(TypeError):
        run_report(None)
