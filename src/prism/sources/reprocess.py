"""Offline-first, auditable scholarly identifier reprocessing.

The runner reads a candidate JSON file and always writes a new timestamped
directory containing ``manifest.json`` and ``summary.json``.  Dry runs never
call HTTP.  Executing resolution requires an explicitly injected
``HttpGetter``; the resulting clients only use the public Crossref, OpenAlex,
and Europe PMC endpoints and expose no header or credential interface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .http import HttpGetter
from .models import FailureKind, SourceFetchError, SourceItem
from .scholarly import (
    CrossrefClient,
    EuropePmcClient,
    OpenAlexClient,
    ScholarlyMetadataClient,
    extract_doi,
    extract_pmcid,
    extract_pmid,
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
    redact_audit_text,
)

CLASSIFICATIONS = (
    "abstract_only",
    "metadata_only",
    "fulltext",
    "blocked",
    "unresolved",
)
_MAX_INPUT_BYTES = 16 * 1024 * 1024


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    normalized = " ".join(value.split())
    return normalized or None


def _text_tuple(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError(f"{name} must be a list of strings")
    result = tuple(_optional_text(item, name) for item in value)
    if any(item is None for item in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return tuple(item for item in result if item is not None)


def _candidate_value(payload: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in payload:
            return payload[name]
    return None


@dataclass(frozen=True, slots=True)
class ReprocessCandidate:
    candidate_id: str
    title: str | None = None
    link: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    authors: tuple[str, ...] = ()
    container_title: str | None = None
    year: int | None = None
    access_level: str | None = None

    def __post_init__(self) -> None:
        candidate_id = _optional_text(self.candidate_id, "candidate_id")
        if candidate_id is None:
            raise ValueError("candidate_id must be a non-empty string")
        object.__setattr__(self, "candidate_id", candidate_id)
        for name in ("title", "link", "container_title"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        object.__setattr__(self, "authors", _text_tuple(self.authors, "authors"))
        if self.year is not None and (
            isinstance(self.year, bool)
            or not isinstance(self.year, int)
            or not 1000 <= self.year <= 9999
        ):
            raise ValueError("year must be a four-digit integer or null")
        if self.access_level is not None and self.access_level not in CLASSIFICATIONS[:-1]:
            raise ValueError("access_level is invalid")
        object.__setattr__(self, "doi", None if self.doi is None else normalize_doi(self.doi))
        object.__setattr__(self, "pmid", None if self.pmid is None else normalize_pmid(self.pmid))
        object.__setattr__(self, "pmcid", None if self.pmcid is None else normalize_pmcid(self.pmcid))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], index: int) -> "ReprocessCandidate":
        candidate_id = _candidate_value(payload, "candidate_id", "id", "source_id")
        if candidate_id is None:
            candidate_id = f"candidate-{index + 1:04d}"
        link = _candidate_value(payload, "link", "url")
        doi = _candidate_value(payload, "doi")
        pmid = _candidate_value(payload, "pmid")
        pmcid = _candidate_value(payload, "pmcid")
        if isinstance(link, str):
            doi = doi or extract_doi(link)
            pmcid = pmcid or extract_pmcid(link)
            pmid = pmid or extract_pmid(link)
        return cls(
            candidate_id=str(candidate_id),
            title=_candidate_value(payload, "title"),  # type: ignore[arg-type]
            link=link,  # type: ignore[arg-type]
            doi=doi,  # type: ignore[arg-type]
            pmid=pmid,  # type: ignore[arg-type]
            pmcid=pmcid,  # type: ignore[arg-type]
            authors=_candidate_value(payload, "authors", "author") or (),  # type: ignore[arg-type]
            container_title=_candidate_value(
                payload, "container_title", "journal", "venue"
            ),  # type: ignore[arg-type]
            year=_candidate_value(payload, "year", "publication_year"),  # type: ignore[arg-type]
            access_level=_candidate_value(payload, "access_level", "classified"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReprocessRun:
    output_dir: Path
    manifest_path: Path
    summary_path: Path
    started_at: datetime
    finished_at: datetime
    total: int
    counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _aware(self.started_at, "started_at")
        _aware(self.finished_at, "finished_at")
        object.__setattr__(self, "counts", tuple(self.counts))


def _load_candidates(path: Path) -> tuple[list[object], str]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("candidate JSON exceeds the size limit")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate input must be valid UTF-8 JSON") from error
    if isinstance(payload, Mapping):
        payload = payload.get("candidates")
    if not isinstance(payload, list):
        raise ValueError("candidate JSON must be a list or contain a candidates list")
    return payload, digest


def _safe_link(value: str | None) -> str | None:
    return None if value is None else redact_audit_text(value)


def _record(
    candidate: ReprocessCandidate,
    *,
    classification: str,
    detail: str,
    item: SourceItem | None = None,
) -> dict[str, object]:
    if classification not in CLASSIFICATIONS:
        raise ValueError("invalid reprocess classification")
    return {
        "candidate_id": redact_audit_text(candidate.candidate_id),
        "title": (
            None if candidate.title is None else redact_audit_text(candidate.title)
        ),
        "input_link": _safe_link(candidate.link),
        "classification": classification,
        "detail": redact_audit_text(detail),
        "doi": item.doi if item is not None else candidate.doi,
        "pmid": item.pmid if item is not None else candidate.pmid,
        "pmcid": item.pmcid if item is not None else candidate.pmcid,
        "resolved_link": _safe_link(item.link) if item is not None else None,
    }


async def _resolve_candidate(
    candidate: ReprocessCandidate,
    client: ScholarlyMetadataClient,
) -> dict[str, object]:
    if candidate.access_level in {"fulltext", "blocked"}:
        return _record(
            candidate,
            classification=candidate.access_level,
            detail="classification preserved from candidate input",
        )
    try:
        if candidate.doi is not None:
            item = await client.fetch(f"DOI: {candidate.doi}")
        elif candidate.pmcid is not None:
            item = await client.fetch(f"PMCID: {candidate.pmcid}")
        elif candidate.pmid is not None:
            item = await client.fetch(f"PMID: {candidate.pmid}")
        elif candidate.title is not None:
            item = await client.fetch_by_title(
                candidate.title,
                link=candidate.link,
                authors=candidate.authors,
                container_title=candidate.container_title,
                year=candidate.year,
            )
        else:
            return _record(
                candidate,
                classification="unresolved",
                detail="candidate has no trusted identifier or title",
            )
    except SourceFetchError as error:
        access_denied = error.kind is FailureKind.HTTP_STATUS and any(
            marker in error.detail for marker in ("status 401", "status 403")
        )
        classification = (
            "blocked"
            if error.kind is FailureKind.BLOCKED or access_denied
            else "unresolved"
        )
        return _record(
            candidate,
            classification=classification,
            detail=f"{error.kind.value}: {error.detail}; source={_safe_link(error.url)}",
        )
    except Exception as error:
        return _record(
            candidate,
            classification="unresolved",
            detail=f"resolver failed ({type(error).__name__})",
        )
    classification = item.access_level or "unresolved"
    if classification not in CLASSIFICATIONS:
        classification = "unresolved"
    return _record(
        candidate,
        classification=classification,
        detail="resolved through public scholarly metadata",
        item=item,
    )


def _dry_record(candidate: ReprocessCandidate) -> dict[str, object]:
    if candidate.access_level in CLASSIFICATIONS[:-1]:
        classification = candidate.access_level
        detail = "classification preserved from candidate input; no HTTP attempted"
    else:
        classification = "unresolved"
        detail = "dry-run: resolution not attempted"
    return _record(candidate, classification=classification, detail=detail)


def _counts(records: list[dict[str, object]]) -> dict[str, int]:
    # Summary values are deliberately derived from the manifest detail rows.
    return {
        classification: sum(
            record["classification"] == classification for record in records
        )
        for classification in CLASSIFICATIONS
    }


def _write_json_new(path: Path, payload: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


async def run_identifier_reprocess(
    input_path: str | Path,
    output_root: str | Path,
    *,
    dry_run: bool = True,
    getter: HttpGetter | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ReprocessRun:
    """Reprocess candidate identifiers into a new, non-overwriting audit run."""
    if not isinstance(dry_run, bool):
        raise TypeError("dry_run must be a bool")
    if not dry_run and getter is None:
        raise ValueError("executing reprocessing requires an explicitly injected HttpGetter")
    effective_clock = clock or (lambda: datetime.now(timezone.utc))
    if not callable(effective_clock):
        raise TypeError("clock must be callable")
    started_at = _aware(effective_clock(), "clock result")
    input_file = Path(input_path).expanduser().resolve()
    output_base = Path(output_root).expanduser().resolve()
    payloads, input_digest = _load_candidates(input_file)

    stamp = started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output_dir = output_base / stamp
    output_base.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=False)

    client = None
    if not dry_run:
        assert getter is not None
        client = ScholarlyMetadataClient(
            CrossrefClient(getter),
            OpenAlexClient(getter),
            EuropePmcClient(getter),
            clock=effective_clock,
        )

    records: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for index, payload in enumerate(payloads):
        fallback = ReprocessCandidate(candidate_id=f"candidate-{index + 1:04d}")
        if not isinstance(payload, Mapping):
            records.append(
                _record(
                    fallback,
                    classification="unresolved",
                    detail="candidate must be a JSON object",
                )
            )
            continue
        try:
            candidate = ReprocessCandidate.from_mapping(payload, index)
            if candidate.candidate_id in seen_ids:
                raise ValueError("candidate_id must be unique")
            seen_ids.add(candidate.candidate_id)
        except (TypeError, ValueError) as error:
            records.append(
                _record(
                    fallback,
                    classification="unresolved",
                    detail=f"invalid candidate ({type(error).__name__}): {error}",
                )
            )
            continue
        if dry_run:
            records.append(_dry_record(candidate))
        else:
            assert client is not None
            records.append(await _resolve_candidate(candidate, client))

    finished_at = _aware(effective_clock(), "clock result")
    if finished_at < started_at:
        raise ValueError("clock moved backwards during reprocessing")
    counts = _counts(records)
    summary = {
        "total": len(records),
        "counts": counts,
        "dry_run": dry_run,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
    }
    manifest = {
        "schema_version": 1,
        "input": {"name": redact_audit_text(input_file.name), "sha256": input_digest},
        **summary,
        "records": records,
    }
    manifest_path = output_dir / "manifest.json"
    summary_path = output_dir / "summary.json"
    _write_json_new(manifest_path, manifest)
    _write_json_new(summary_path, summary)
    return ReprocessRun(
        output_dir=output_dir,
        manifest_path=manifest_path,
        summary_path=summary_path,
        started_at=started_at,
        finished_at=finished_at,
        total=len(records),
        counts=tuple(counts.items()),
    )


__all__ = [
    "CLASSIFICATIONS",
    "ReprocessCandidate",
    "ReprocessRun",
    "run_identifier_reprocess",
]
