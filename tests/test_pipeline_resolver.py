"""Focused tests for the store-backed material resolver (module: pipeline.resolver)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.config import PathConfig
from prism.events import Event
from prism.ingestion import IngestionResult
from prism.pipeline.resolver import StoreMaterialResolver
from prism.store import EvidenceStore


T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)

DOCUMENT = """---
source: example.test
title: Policy update
published_at: 2026-08-30T09:00:00+00:00
fetched_at: 2026-08-31T12:00:00+00:00
type: policy
source_id: mat-1
case_tags: ["case-1"]
access_level: fulltext
---

The agency published the revised policy.
"""


def make_environment(tmp_path: Path) -> tuple[EvidenceStore, PathConfig, Path]:
    paths = PathConfig().resolve(tmp_path)
    paths.corpus_dir.mkdir(parents=True, exist_ok=True)
    corpus_file = paths.corpus_dir / "2026-08" / "example.test" / "policy-update.md"
    corpus_file.parent.mkdir(parents=True, exist_ok=True)
    corpus_file.write_text(DOCUMENT, encoding="utf-8")
    store = EvidenceStore(paths)
    store.initialize()
    store.index_file(corpus_file)
    return store, paths, corpus_file


def make_event(material_id: object = "mat-1") -> Event:
    return Event(
        event_id="evt-1",
        event_type="material.ingested",
        occurred_at=T0,
        payload={"material_id": material_id, "corpus_path": "unused"},
        correlation_id="mat-1",
    )


def test_resolve_rebuilds_the_ingestion_result_from_the_store(tmp_path):
    store, paths, corpus_file = make_environment(tmp_path)
    resolver = StoreMaterialResolver(store, paths)

    result = resolver.resolve("mat-1")

    assert isinstance(result, IngestionResult)
    assert result.material.id == "mat-1"
    assert result.material.source == "example.test"
    assert result.material.content == "The agency published the revised policy."
    assert result.material.case_tags == ("case-1",)
    assert result.material.access_level == "fulltext"
    assert result.corpus_path.is_absolute()
    assert result.corpus_path == corpus_file.resolve()
    assert result.corpus_path.is_file()
    assert result.used_ocr is False
    assert result.extracted_via == "store"


def test_resolve_anchors_the_raw_path_and_defaults_missing_raw_to_corpus(tmp_path):
    store, paths, _ = make_environment(tmp_path)
    resolver = StoreMaterialResolver(store, paths)

    result = resolver.resolve("mat-1")
    # This document carries no raw_path: the corpus file is the fallback.
    assert result.raw_path == result.corpus_path


def test_resolve_unknown_material_raises_lookup_error(tmp_path):
    store, paths, _ = make_environment(tmp_path)
    resolver = StoreMaterialResolver(store, paths)
    with pytest.raises(LookupError, match="mat-unknown"):
        resolver.resolve("mat-unknown")
    with pytest.raises(ValueError):
        resolver.resolve("   ")


def test_event_callable_reads_material_id_from_the_payload(tmp_path):
    store, paths, _ = make_environment(tmp_path)
    resolver = StoreMaterialResolver(store, paths)

    resolved = resolver(make_event())
    assert resolved.material.id == "mat-1"
    assert resolved == resolver.resolve("mat-1")

    for bad in (None, "", "  ", 42):
        with pytest.raises(ValueError):
            resolver(make_event(material_id=bad))


def test_constructor_validates_dependencies(tmp_path):
    store, paths, _ = make_environment(tmp_path)
    with pytest.raises(TypeError):
        StoreMaterialResolver(object(), paths)
    with pytest.raises(TypeError):
        StoreMaterialResolver(store, "not-paths")
    assert StoreMaterialResolver(store, paths) is not None
