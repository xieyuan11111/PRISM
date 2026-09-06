"""WebUI workbench Phase A: the material journey projection seam (WB-2/WB-3).

These tests cover the dependency-free journey controller and projection
(:mod:`prism.webui.journey`): the facade's read-only
:class:`~prism.api.facade.MaterialJourneyView` is mapped onto the fixed
seven-step journey (staged → ingested → indexed → extracted → merged →
graph_written → analyzed) using only recorded audit data.  The core
invariant under test is semantic honesty (H-4/WB-3.6): pending, partial,
unknown and failure states are never rendered as success; skipped stages
show their reason instead of a failure; and the retry entry point delegates
to ``PrismAPI.process_material`` without rewriting outcomes.  Everything is
offline with synthetic view objects — no NiceGUI, no pipeline runtime.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import pytest

from prism.api.facade import MaterialJourneyView, ProcessMaterialResult
from prism.pipeline.outcomes import PipelineRunAudit, StageAuditRecord
from prism.pipeline.service import PipelineFailure, PipelineRun, PipelineStage
from prism.store import IndexEntry, IndexOutcome


UTC = timezone.utc
PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
STARTED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
FINISHED = datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC)
FAILED_AT = datetime(2026, 9, 1, 12, 0, 3, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def _make_entry() -> IndexEntry:
    return IndexEntry(
        source_id="mat-1",
        title="Policy update",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content="The agency published the revised policy.",
        path="corpus/doc-mat-1.md",
        content_hash="0" * 64,
    )


def _run(material_id="mat-1", graph_detail=None):
    return PipelineRun(
        material_id=material_id,
        status="completed",
        stages=(
            _index_stage(),
            _extract_stage(),
            _graph_stage(graph_detail),
        ),
        started_at=STARTED,
        finished_at=FINISHED,
    )


def _index_stage():
    return PipelineStage(
        "index", "indexed", IndexOutcome(_make_entry(), "indexed")
    )


def _extract_stage():
    from prism.extraction import ExtractionResult

    return PipelineStage("extract", "extracted", ExtractionResult(case=None))


def _graph_stage(detail=None):
    from prism.graph import GraphWriteResult

    return PipelineStage(
        "graph",
        "written",
        GraphWriteResult((), ("merged-episode",), ()),
        detail=detail or "merged case write across 2 accumulated material(s)",
    )


def _audit(report_version_id="rv-1"):
    return PipelineRunAudit(
        material_id="mat-1",
        status="completed",
        stages=(
            StageAuditRecord("index", "indexed"),
            StageAuditRecord("extract", "extracted"),
            StageAuditRecord("graph", "written", detail="merged case write"),
        ),
        started_at=STARTED,
        finished_at=FINISHED,
        correlation_id="corr-1",
        report_version_id=report_version_id,
    )


def committed_view(**overrides) -> MaterialJourneyView:
    fields = dict(
        material_id="mat-1",
        display_name="Policy update",
        source_format="md",
        case_id="case-1",
        raw_path="raw/mat-1.md",
        corpus_path="corpus/doc-mat-1.md",
        content_hash="0" * 64,
        fetched_at=FINISHED,
        occurred_at=FINISHED,
        lifecycle_status="committed",
        run=_run(),
        run_audit=_audit(),
        failure=None,
        mechanism_status="pass",
        semantic_status="unknown",
        evidence_gap_count=1,
        evidence_gaps=("evidence_location_failed on node: quote missing",),
        unresolved_conflicts=(),
        report_version_id="rv-1",
    )
    fields.update(overrides)
    return MaterialJourneyView(**fields)


# ------------------------------------------------------------- step mapping


def test_steps_of_a_fully_processed_material():
    from prism.webui.journey import journey_steps

    steps = journey_steps(committed_view())

    assert [step["step"] for step in steps] == [
        "staged", "ingested", "indexed", "extracted", "merged",
        "graph_written", "analyzed",
    ]
    by_step = {step["step"]: step for step in steps}
    assert by_step["ingested"]["status"] == "completed"
    assert by_step["indexed"]["status"] == "completed"
    assert by_step["extracted"]["status"] == "completed"
    assert by_step["merged"]["status"] == "completed"
    assert by_step["graph_written"]["status"] == "completed"
    assert by_step["analyzed"]["status"] == "completed"
    assert by_step["merged"]["detail"] == "case case-1"
    assert by_step["analyzed"]["detail"] == "report version rv-1"
    assert by_step["ingested"]["detail"].startswith("raw: raw/mat-1.md")
    # Staging is a transient WebUI spool with no durable audit, so the
    # staged step stays unknown even for a fully processed material.
    assert by_step["staged"]["status"] == "unknown"


def test_staged_step_never_claims_a_browser_upload_without_audit():
    # Every indexed material has fetched_at; that must never be rendered
    # as proof of browser staging, because the staging spool leaves no
    # durable audit record (H-6: no invented facts).
    from prism.webui.journey import journey_steps

    view = committed_view()
    assert view.fetched_at is not None

    staged_step = {
        step["step"]: step for step in journey_steps(view)
    }["staged"]

    assert staged_step["status"] == "unknown"
    assert staged_step["time"] is None
    assert "no durable audit" in staged_step["detail"]


def test_ingested_step_requires_both_raw_and_corpus_copies():
    from prism.webui.journey import journey_steps

    corpus_only = committed_view(raw_path=None)
    raw_only = committed_view(corpus_path=None)

    for view in (corpus_only, raw_only):
        step = {step["step"]: step for step in journey_steps(view)}[
            "ingested"
        ]
        assert step["status"] == "unknown"
        assert "not recorded" in step["detail"]

    both = {
        step["step"]: step for step in journey_steps(committed_view())
    }["ingested"]
    assert both["status"] == "completed"


def test_step_times_come_only_from_recorded_audit_data():
    # No per-step timestamps are recorded anywhere: the run's finished_at is
    # never re-stamped onto stages, and fetched_at is the SOURCE's crawl
    # time — not the raw/corpus normalization completion time — so even the
    # ingested step shows no time without an explicit recording (the one
    # exception, the ledger's failed_at on the failed step, is pinned by
    # the failure test below).
    from prism.webui.journey import journey_steps

    view = committed_view()
    assert view.fetched_at is not None

    steps = journey_steps(view)

    assert [step["time"] for step in steps] == [None] * 7


def test_skipped_graph_stages_show_their_reason_not_a_failure():
    from prism.webui.journey import journey_steps

    skipped_run = PipelineRun(
        material_id="mat-1",
        status="completed",
        stages=(
            _index_stage(),
            _extract_stage(),
            PipelineStage(
                "graph", "skipped", None,
                detail="extraction produced no case",
            ),
        ),
        started_at=STARTED,
        finished_at=FINISHED,
    )
    view = committed_view(run=skipped_run, case_id=None, run_audit=None,
                          report_version_id=None)

    steps = {step["step"]: step for step in journey_steps(view)}

    assert steps["graph_written"]["status"] == "skipped"
    assert "extraction produced no case" in steps["graph_written"]["detail"]
    assert steps["merged"]["status"] == "skipped"
    assert steps["merged"]["status"] != "failed"


def test_a_view_without_evidence_shows_unknown_everywhere():
    from prism.webui.journey import journey_steps

    view = MaterialJourneyView(material_id="mat-1")

    steps = journey_steps(view)

    assert [step["status"] for step in steps] == ["unknown"] * 7
    assert all(step["status"] != "success" for step in steps)


def test_failure_marks_the_failed_step_with_the_error_type():
    from prism.webui.journey import journey_steps

    failure = PipelineFailure(
        material_id="mat-1",
        stage="extract",
        error_type="RuntimeError",
        message="extractor exploded",
        failed_at=FAILED_AT,
    )
    view = committed_view(
        lifecycle_status="failed",
        mechanism_status="fail",
        run=None,
        run_audit=PipelineRunAudit(
            "mat-1", "failed",
            stages=(StageAuditRecord("index", "indexed"),),
            finished_at=FAILED_AT,
        ),
        failure=failure,
        report_version_id=None,
        evidence_gap_count=None,
        evidence_gaps=(),
    )

    steps = {step["step"]: step for step in journey_steps(view)}

    assert steps["extracted"]["status"] == "failed"
    assert "RuntimeError" in steps["extracted"]["detail"]
    # The failed step carries the one recorded per-step time: the
    # ledger's failure timestamp.
    assert steps["extracted"]["time"] == FAILED_AT.isoformat()
    assert steps["indexed"]["status"] == "completed"
    assert steps["indexed"]["time"] is None
    assert steps["analyzed"]["status"] == "unknown"


def test_post_restart_views_project_from_the_durable_audit():
    from prism.webui.journey import journey_steps

    view = committed_view(run=None, evidence_gap_count=None, evidence_gaps=())

    steps = {step["step"]: step for step in journey_steps(view)}

    assert steps["indexed"]["status"] == "completed"
    assert steps["extracted"]["status"] == "completed"
    assert steps["graph_written"]["status"] == "completed"
    assert steps["analyzed"]["detail"] == "report version rv-1"


def test_unrecognized_stage_statuses_never_render_as_completed():
    # A recorded stage row whose status is not an explicit completion
    # status must show unknown — never completed (H-4: no fabricated
    # success from partially written or foreign audit data).
    from prism.webui.journey import journey_steps

    audit = PipelineRunAudit(
        material_id="mat-1",
        status="completed",
        stages=(
            StageAuditRecord("index", "pending"),
            StageAuditRecord("extract", "extracted"),
            StageAuditRecord("graph", "written"),
        ),
        started_at=STARTED,
        finished_at=FINISHED,
    )
    view = committed_view(run=None, run_audit=audit)

    steps = {step["step"]: step for step in journey_steps(view)}

    assert steps["indexed"]["status"] == "unknown"
    assert steps["extracted"]["status"] == "completed"
    assert steps["graph_written"]["status"] == "completed"


def test_audit_view_status_is_whitelisted_to_explicit_values():
    # The journey's audit status field only ever shows an explicitly
    # recorded "completed"/"skipped"; anything else — including a failed
    # audit or a foreign value — projects as unknown and never as
    # completed.  The failure itself is carried by the failure triple.
    from prism.webui.journey import journey_view_data

    failed_audit = PipelineRunAudit(
        "mat-1", "failed",
        stages=(StageAuditRecord("index", "indexed"),),
        finished_at=FAILED_AT,
    )
    data = journey_view_data(committed_view(run_audit=failed_audit))

    assert data["audit"]["status"] == "unknown"

    completed = journey_view_data(committed_view())
    assert completed["audit"]["status"] == "completed"


# ------------------------------------------------------------- view data


def test_journey_view_data_is_json_safe_and_complete():
    from prism.webui.journey import journey_view_data

    data = journey_view_data(committed_view())

    assert json.loads(json.dumps(data)) == data
    assert data["material_id"] == "mat-1"
    assert data["lifecycle_status"] == "committed"
    assert data["ui_status"] == "success"
    assert data["mechanism_status"] == "pass"
    assert data["semantic_status"] == "unknown"
    assert data["evidence_gap_count"] == 1
    assert data["steps"]
    assert data["run"]["material_id"] == "mat-1"
    assert data["audit"]["status"] == "completed"
    assert data["failure"] is None
    assert "result" not in json.dumps(data["run"]["stages"])


def test_failed_views_carry_the_failure_triple():
    from prism.webui.journey import journey_view_data

    failure = PipelineFailure(
        material_id="mat-1",
        stage="graph",
        error_type="MaterialCaseConflict",
        message="material already bound to case case-9",
        failed_at=FAILED_AT,
    )
    view = committed_view(
        lifecycle_status="failed", mechanism_status="fail", failure=failure,
        run=None, report_version_id=None,
    )

    data = journey_view_data(view)

    assert data["ui_status"] == "failure"
    assert data["failure"]["stage"] == "graph"
    assert data["failure"]["error_type"] == "MaterialCaseConflict"
    assert data["failure"]["message"] == (
        "material already bound to case case-9"
    )


def test_material_rows_never_show_success_for_unfinished_states():
    from prism.webui.journey import material_row

    pending = material_row(MaterialJourneyView(
        material_id="mat-p", lifecycle_status="pending",
    ))
    unknown = material_row(MaterialJourneyView(material_id="mat-u"))
    failed = material_row(MaterialJourneyView(
        material_id="mat-f", lifecycle_status="failed",
    ))

    assert pending["ui_status"] == "loading"
    assert unknown["ui_status"] == "unknown"
    assert failed["ui_status"] == "failure"
    assert material_row(committed_view())["ui_status"] == "success"


def test_lifecycle_ui_status_mapping():
    from prism.webui.status import lifecycle_ui_status

    assert lifecycle_ui_status("committed") == "success"
    assert lifecycle_ui_status("failed") == "failure"
    assert lifecycle_ui_status("pending") == "loading"
    assert lifecycle_ui_status("unknown") == "unknown"
    assert lifecycle_ui_status(None) == "unknown"


def test_journey_markdown_renders_steps_and_paths():
    from prism.webui.journey import journey_markdown, journey_view_data

    text = journey_markdown(journey_view_data(committed_view()))

    assert "mat-1" in text
    assert "staged" in text
    assert "raw/mat-1.md" in text
    assert "corpus/doc-mat-1.md" in text
    assert "rv-1" in text
    assert "committed" in text
    assert "partial" not in text


def test_journey_markdown_flags_partial_semantics():
    from prism.webui.journey import journey_markdown, journey_view_data

    view = committed_view(semantic_status="partial")
    text = journey_markdown(journey_view_data(view))

    assert "partial" in text


# --------------------------------------------------------- controller seam


class FakeJourneyFacade:
    def __init__(self, views=(), by_id=None, retry_result=None,
                 retry_error=None):
        self.views = tuple(views)
        self.by_id = dict(by_id or {})
        self.retry_result = retry_result
        self.retry_error = retry_error
        self.journey_calls = []
        self.journeys_calls = []
        self.retry_calls = []

    async def material_journey(self, material_id):
        self.journey_calls.append(material_id)
        if material_id not in self.by_id:
            raise LookupError(f"material not found: {material_id}")
        return self.by_id[material_id]

    async def material_journeys(self, *, case_id=None, status=None):
        self.journeys_calls.append({"case_id": case_id, "status": status})
        return self.views

    async def process_material(self, source, metadata=None, *,
                               target_case=None):
        self.retry_calls.append(source)
        if self.retry_error is not None:
            raise self.retry_error
        return self.retry_result


def _retry_result():
    return ProcessMaterialResult(
        material_id="mat-1",
        pipeline=_run(),
        case_id="case-1",
        case_outcome=None,
        warnings=(),
        replayed=True,
    )


def _failed_view() -> MaterialJourneyView:
    failure = PipelineFailure(
        material_id="mat-1",
        stage="extract",
        error_type="RuntimeError",
        message="extractor exploded",
        failed_at=FAILED_AT,
    )
    return committed_view(
        lifecycle_status="failed",
        mechanism_status="fail",
        run=None,
        run_audit=PipelineRunAudit(
            "mat-1", "failed",
            stages=(StageAuditRecord("index", "indexed"),),
            finished_at=FAILED_AT,
        ),
        failure=failure,
        report_version_id=None,
        evidence_gap_count=None,
        evidence_gaps=(),
    )


def _controller(facade=None):
    from prism.webui.journey import MaterialJourneyController

    return MaterialJourneyController(
        facade if facade is not None else FakeJourneyFacade()
    )


def test_controller_rejects_incomplete_facades():
    from prism.webui.journey import MaterialJourneyController

    class Empty:
        pass

    with pytest.raises(TypeError, match="material_journey"):
        MaterialJourneyController(Empty())


def test_load_journeys_projects_rows_and_count():
    controller = _controller(FakeJourneyFacade(views=(
        committed_view(),
        MaterialJourneyView(material_id="mat-2", lifecycle_status="failed"),
    )))

    payload = run(controller.load_journeys())

    assert payload["count"] == 2
    assert [row["material_id"] for row in payload["materials"]] == [
        "mat-1", "mat-2",
    ]
    assert payload["materials"][1]["ui_status"] == "failure"


def test_load_journeys_forwards_filters_after_validation():
    facade = FakeJourneyFacade()
    controller = _controller(facade)

    run(controller.load_journeys(case_id="case-1", status="failed"))

    assert facade.journeys_calls == [
        {"case_id": "case-1", "status": "failed"}
    ]
    with pytest.raises(ValueError, match="status"):
        run(controller.load_journeys(status="green"))
    with pytest.raises(ValueError, match="case_id"):
        run(controller.load_journeys(case_id=" "))
    assert len(facade.journeys_calls) == 1


def test_load_journey_returns_the_full_projection():
    controller = _controller(FakeJourneyFacade(by_id={
        "mat-1": committed_view(),
    }))

    data = run(controller.load_journey("mat-1"))

    assert data["material_id"] == "mat-1"
    assert data["steps"][0]["step"] == "staged"

    with pytest.raises(ValueError):
        run(controller.load_journey(""))


def test_retry_delegates_to_process_material_by_id():
    facade = FakeJourneyFacade(
        retry_result=_retry_result(), by_id={"mat-1": _failed_view()}
    )
    controller = _controller(facade)

    view = run(controller.retry("mat-1"))

    assert facade.retry_calls == ["mat-1"]
    assert view["material_id"] == "mat-1"
    assert view["replayed"] is True


def test_retry_only_runs_for_the_failed_lifecycle():
    # Committed, pending and unknown lifecycles are refused before any
    # facade write: re-processing a committed material could rewrite its
    # recorded evidence and break the append's report-version link, and a
    # pending material is already in flight.
    facade = FakeJourneyFacade(
        retry_result=_retry_result(),
        by_id={
            "mat-c": committed_view(),
            "mat-p": MaterialJourneyView(
                material_id="mat-p", lifecycle_status="pending"
            ),
            "mat-u": MaterialJourneyView(material_id="mat-u"),
        },
    )
    controller = _controller(facade)

    for material_id in ("mat-c", "mat-p", "mat-u"):
        with pytest.raises(ValueError, match="failed"):
            run(controller.retry(material_id))

    assert facade.retry_calls == []
    assert facade.journey_calls == ["mat-c", "mat-p", "mat-u"]


def test_retry_of_an_unknown_material_never_reaches_the_pipeline():
    facade = FakeJourneyFacade(retry_result=_retry_result())
    controller = _controller(facade)

    with pytest.raises(LookupError):
        run(controller.retry("mat-x"))

    assert facade.retry_calls == []


def test_retry_failures_propagate_without_fake_success():
    from prism.pipeline import PipelineError

    facade = FakeJourneyFacade(
        retry_error=PipelineError("still failing", stage="extract",
                                  material_id="mat-1"),
        by_id={"mat-1": _failed_view()},
    )
    controller = _controller(facade)

    with pytest.raises(PipelineError):
        run(controller.retry("mat-1"))


def test_is_terminal_distinguishes_processing_from_done():
    from prism.webui.journey import MaterialJourneyController

    assert MaterialJourneyController.is_terminal({"lifecycle_status": "committed"})
    assert MaterialJourneyController.is_terminal({"lifecycle_status": "failed"})
    assert not MaterialJourneyController.is_terminal(
        {"lifecycle_status": "pending"}
    )
    assert not MaterialJourneyController.is_terminal(
        {"lifecycle_status": "unknown"}
    )


# ------------------------------------------------------ import / page seam


def test_importing_the_module_never_imports_nicegui():
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "nicegui" or name.startswith("nicegui.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        from prism.webui import journey

        assert callable(journey.MaterialJourneyController)
        assert not any(
            name == "nicegui" or name.startswith("nicegui.")
            for name in sys.modules
        )
    finally:
        sys.modules.update(saved)
