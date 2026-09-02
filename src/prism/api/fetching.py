"""Safe raw spooling and outcome contracts for fetched public sources.

``SourceItem`` objects are in-memory text with no file interface of their
own, so before :class:`~prism.ingestion.IngestionService` can normalize a
fetched item its original body (full content, or the summary when the source
carried none) is written into a spool directory under the PRISM raw tree.
The spool write is deliberately minimal and defensive: file names derive
only from a SHA-256 digest of the item's deduplication key (stable across
refetches, immune to path traversal), the resolved destination must stay
inside the spool directory, and the file appears via an atomic
:func:`os.replace` so readers never observe a half-written body.  The corpus
itself is never written here — ingestion stays the single corpus producer.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from prism.sources import SourceItem


SPOOL_DIRNAME = "spool"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_path(name: str, value: Path) -> None:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a Path")


def spool_source_item(item: SourceItem, spool_dir: Path) -> Path:
    """Write one item's original body to ``spool_dir`` and return its path.

    The file name is ``source-<digest>.md`` where ``<digest>`` is the first
    32 hex characters of the SHA-256 of the item's deduplication key, so the
    same item always lands in the same file while different items never
    collide; nothing derived from the item text reaches the file system
    unhashed.
    """
    if not isinstance(item, SourceItem):
        raise TypeError("item must be a SourceItem")
    _require_path("spool_dir", spool_dir)

    digest = hashlib.sha256(item.dedup_key.encode("utf-8")).hexdigest()[:32]
    destination = spool_dir / f"source-{digest}.md"
    if not destination.resolve().is_relative_to(spool_dir.resolve()):
        raise ValueError(f"spool path escapes its directory: {destination}")

    body = item.content or item.summary or ""
    if not body.strip() and item.access_level == "metadata_only":
        authors = getattr(item, "authors", ())
        container_title = getattr(item, "container_title", None)
        body = (
            "[Scholarly metadata record]\n\n"
            f"Title: {item.title}\n"
            f"Source: {item.source}\n"
            f"DOI: {getattr(item, 'doi', None) or 'unknown'}\n"
            + (f"Authors: {', '.join(authors)}\n" if authors else "")
            + (f"Venue: {container_title}\n" if container_title else "")
            + "Full text was not available through the public metadata source."
        )
    spool_dir.mkdir(parents=True, exist_ok=True)
    staging = spool_dir / f".{destination.name}.{os.getpid()}.tmp"
    try:
        with staging.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        os.replace(staging, destination)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return destination


@dataclass(frozen=True, slots=True)
class SourceItemReport:
    """What happened to one fetched item inside the application.

    ``access_level`` reports the evidence level of the ingested material
    (``fulltext``, ``abstract_only``, ``metadata_only``, or ``None`` when the
    source carried no level) so callers can see that a scholarly fallback
    item was only ever an abstract or bibliographic record, never full text.
    """

    title: str
    source: str
    link: str | None
    material_id: str
    spool_path: Path
    raw_path: Path
    corpus_path: Path
    pipeline: object | None = None  # PipelineRun when the pipeline was run
    access_level: str | None = None

    def __post_init__(self) -> None:
        _require_text("title", self.title)
        _require_text("source", self.source)
        if self.link is not None:
            _require_text("link", self.link)
        _require_text("material_id", self.material_id)
        for name in ("spool_path", "raw_path", "corpus_path"):
            _require_path(name, getattr(self, name))
        if self.access_level is not None:
            _require_text("access_level", self.access_level)


@dataclass(frozen=True, slots=True)
class SourceFetchReport:
    """The application-level outcome of fetching one source URL."""

    url: str
    fetched_at: datetime
    items: tuple[SourceItemReport, ...] = ()
    duplicate_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("url", self.url)
        if not isinstance(self.fetched_at, datetime):
            raise TypeError("fetched_at must be a datetime")
        object.__setattr__(self, "items", tuple(self.items))
        for item in self.items:
            if not isinstance(item, SourceItemReport):
                raise TypeError("items must contain only SourceItemReport objects")
        object.__setattr__(self, "duplicate_keys", tuple(self.duplicate_keys))
        for key in self.duplicate_keys:
            _require_text("duplicate key", key)


@dataclass(frozen=True, slots=True)
class SourceURLFailure:
    """One recorded failure inside a batch, kept instead of faked success."""

    url: str
    kind: str  # FailureKind value, or the exception class name
    detail: str

    def __post_init__(self) -> None:
        _require_text("url", self.url)
        _require_text("kind", self.kind)
        _require_text("detail", self.detail)


@dataclass(frozen=True, slots=True)
class SourceBatchReport:
    """Per-URL outcomes of a multi-source fetch: successes plus failures."""

    reports: tuple[SourceFetchReport, ...] = ()
    failures: tuple[SourceURLFailure, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reports", tuple(self.reports))
        for report in self.reports:
            if not isinstance(report, SourceFetchReport):
                raise TypeError("reports must contain only SourceFetchReport objects")
        object.__setattr__(self, "failures", tuple(self.failures))
        for failure in self.failures:
            if not isinstance(failure, SourceURLFailure):
                raise TypeError("failures must contain only SourceURLFailure objects")


__all__ = [
    "SPOOL_DIRNAME",
    "SourceBatchReport",
    "SourceFetchReport",
    "SourceItemReport",
    "SourceURLFailure",
    "spool_source_item",
]
