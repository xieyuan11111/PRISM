"""Rebuild :class:`~prism.ingestion.IngestionResult` objects for indexed materials.

The event-driven pipeline starts from a ``material.ingested`` event, which
carries only the material id.  :class:`StoreMaterialResolver` resolves that
id against the authoritative evidence store and rebuilds the exact ingestion
result the pipeline expects — the same material the indexer recorded, with
the corpus file anchored back to an absolute path under the PRISM home, so
``index → extract → graph`` can run from the event alone.  No network, no
LLM, and no state beyond the injected store.
"""

from __future__ import annotations

from pathlib import Path

from prism.config import PathConfig
from prism.domain import Material
from prism.events import Event
from prism.ingestion import IngestionResult
from prism.store import EvidenceStore, IndexEntry


def material_from_entry(entry: IndexEntry) -> Material:
    """Rebuild the domain material recorded in one index entry."""
    return Material(
        id=entry.source_id,
        title=entry.title,
        source=entry.source,
        published_at=entry.published_at,
        fetched_at=entry.fetched_at,
        type=entry.type,
        content=entry.content,
        original_format=entry.original_format,
        ocr=entry.ocr,
        extracted_via=entry.extracted_via,
        raw_path=entry.raw_path,
        case_tags=entry.case_tags,
        url=entry.url,
        retrieval_level=entry.retrieval_level,
        access_level=entry.access_level,
        doi=entry.doi,
        authors=entry.authors,
        container_title=entry.container_title,
        pmid=entry.pmid,
        pmcid=entry.pmcid,
    )


class StoreMaterialResolver:
    """Resolve material ids (or ingest events) to ingestion results."""

    def __init__(self, store: EvidenceStore, paths: PathConfig) -> None:
        if store is None or not callable(getattr(store, "get", None)):
            raise TypeError("store must provide get()")
        if not isinstance(paths, PathConfig):
            raise TypeError("paths must be a PathConfig")
        self._store = store
        self._paths = paths

    @property
    def _project_root(self) -> Path:
        # The store records corpus paths relative to the directory that
        # contains the corpus (the PRISM project root).
        return self._paths.corpus_dir.parent

    def _anchor(self, value: str | None, fallback: Path) -> Path:
        if value is None or not value.strip():
            return fallback
        candidate = Path(value)
        return candidate if candidate.is_absolute() else self._project_root / candidate

    def resolve(self, material_id: str) -> IngestionResult:
        """Rebuild the ingestion result of one indexed material."""
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        entry = self._store.get(material_id)
        if entry is None:
            raise LookupError(f"material not found: {material_id}")
        corpus_path = self._anchor(entry.path, self._paths.corpus_dir / "missing.md")
        return IngestionResult(
            material=material_from_entry(entry),
            raw_path=self._anchor(entry.raw_path, corpus_path),
            corpus_path=corpus_path,
            used_ocr=bool(entry.ocr),
            # The result is rebuilt from the index rather than a live
            # ingestion pass; audit trails say so instead of guessing.
            extracted_via=entry.extracted_via or "store",
        )

    def __call__(self, event: Event) -> IngestionResult:
        """Resolve the material a ``material.ingested`` event announces."""
        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        material_id = event.payload.get("material_id")
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError(
                "material.ingested payload must carry a non-empty material_id"
            )
        return self.resolve(material_id)


__all__ = ["StoreMaterialResolver", "material_from_entry"]
