"""Focused tests for the durable pipeline-outcome ledger (pipeline.outcomes)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.config import PathConfig
from prism.pipeline.outcomes import PipelineOutcome, PipelineOutcomeLedger


T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 1, 8, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 1, 8, 2, tzinfo=timezone.utc)


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)


def failed(material_id: str = "mat-1", occurred_at: datetime = T0) -> PipelineOutcome:
    return PipelineOutcome(
        material_id,
        "failed",
        occurred_at,
        stage="extract",
        error_type="RuntimeError",
        message="extractor exploded",
    )


def committed(material_id: str = "mat-2", occurred_at: datetime = T1) -> PipelineOutcome:
    return PipelineOutcome(
        material_id,
        "committed",
        occurred_at,
        correlation_id=f"corr-{material_id}",
    )


def test_ledger_records_and_decodes_terminal_outcomes(tmp_path):
    ledger = PipelineOutcomeLedger(make_paths(tmp_path))
    try:
        assert ledger.entries() == ()
        ledger.record(failed())
        ledger.record(committed())

        entries = ledger.entries()
        assert [entry.material_id for entry in entries] == ["mat-1", "mat-2"]
        assert entries[0] == failed()
        assert entries[0].status == "failed"
        assert entries[0].stage == "extract"
        assert entries[0].error_type == "RuntimeError"
        assert entries[0].message == "extractor exploded"
        assert entries[0].occurred_at == T0
        assert entries[0].occurred_at.tzinfo is not None
        assert entries[1] == committed()
        assert entries[1].correlation_id == "corr-mat-2"
    finally:
        ledger.close()


def test_ledger_upserts_one_current_outcome_per_material(tmp_path):
    ledger = PipelineOutcomeLedger(make_paths(tmp_path))
    try:
        ledger.record(failed())
        # A safe retry replaces the stale failure with the committed outcome.
        ledger.record(
            PipelineOutcome("mat-1", "committed", T2, correlation_id="corr-1")
        )
        entries = ledger.entries()
        assert len(entries) == 1
        assert entries[0].material_id == "mat-1"
        assert entries[0].status == "committed"
        assert entries[0].occurred_at == T2
        assert entries[0].error_type is None
        # The stale failure fields are gone, never mixed into the success.
        assert entries[0].message is None
        assert entries[0].stage is None
    finally:
        ledger.close()


def test_ledger_entries_survive_close_and_reopen(tmp_path):
    ledger = PipelineOutcomeLedger(make_paths(tmp_path))
    ledger.record(failed())
    ledger.record(committed())
    ledger.close()

    reopened = PipelineOutcomeLedger(make_paths(tmp_path))
    try:
        entries = reopened.entries()
        assert [entry.material_id for entry in entries] == ["mat-1", "mat-2"]
        assert reopened.entries()[0] == failed()
        assert reopened.entries()[1] == committed()
    finally:
        reopened.close()


def test_ledger_validates_inputs_and_close_is_idempotent(tmp_path):
    ledger = PipelineOutcomeLedger(make_paths(tmp_path))
    with pytest.raises(TypeError):
        ledger.record(None)
    with pytest.raises(ValueError):
        ledger.record(PipelineOutcome("mat-1", "pending", T0))
    assert ledger.entries() == ()
    ledger.close()
    ledger.close()  # idempotent


def test_ledger_shares_the_evidence_store_database_file(tmp_path):
    paths = make_paths(tmp_path)
    ledger = PipelineOutcomeLedger(paths)
    ledger.record(failed())
    ledger.close()
    database = paths.data_dir / "index.db"
    assert database.is_file()
