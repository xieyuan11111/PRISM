"""Input normalization for Markdown and PDF materials.

PRISM module 2: multi-format ingestion.  Markdown input is normalized (line
endings, trailing whitespace) and completed with a standard frontmatter block;
PDF input is routed through an injectable text extractor with an optional OCR
fallback when the extracted text is too short (scanned documents).  The
original input is copied to ``raw_dir`` while the normalized Markdown document
is written to ``corpus_dir``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from prism.config import PathConfig
from prism.domain import Material


class TextExtractor(Protocol):
    """Anything that can produce text from a source file."""

    name: str

    def extract(self, path: Path) -> str: ...


def _normalize_line_endings(text: str) -> str:
    """Collapse CR, CRLF, and Windows-doubled CRLF to plain LF."""
    return re.sub(r"\r\r\n|\r\n|\r", "\n", text)


def content_hash(text: str) -> str:
    """Return a stable SHA-256 digest for text, insensitive to line endings."""
    return hashlib.sha256(_normalize_line_endings(text).encode("utf-8")).hexdigest()


def stable_material_id(source: str, content: str) -> str:
    """Return a deterministic identifier for a normalized source/content pair."""
    normalized_source = source.strip().lower().rstrip(".")
    digest = hashlib.sha256(
        f"{normalized_source}\0{content_hash(content)}".encode("utf-8")
    ).hexdigest()[:24]
    return f"mat_{digest}"


def _parse_scalar(raw: str) -> Any:
    """Parse a single frontmatter scalar (JSON subset of YAML)."""
    value = raw.strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---`` delimited frontmatter from a Markdown document body.

    Returns ``(frontmatter, body)``.  ``frontmatter`` is a plain dict with
    JSON-scalar values (strings, booleans, numbers, ``None``, lists); a block
    list written as ``key:\\n  - item`` becomes a list value.  When the text
    has no frontmatter block, returns ``({}, normalized_text)``.
    """
    normalized = _normalize_line_endings(text.lstrip("\ufeff"))
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, normalized

    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    closing = len(lines)
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            closing = index
            break
        if line.startswith(("  - ", "- ")):
            item = _parse_scalar(line.strip()[2:])
            if current_key is None:
                raise ValueError("frontmatter list item has no preceding key")
            frontmatter.setdefault(current_key, []).append(item)
            continue
        if ": " not in line and line.endswith(":"):
            current_key = line[:-1].strip()
            if current_key:
                frontmatter[current_key] = []
            continue
        if ": " in line:
            key, _, raw = line.partition(": ")
            current_key = key.strip()
            if current_key:
                frontmatter[current_key] = _parse_scalar(raw)
            continue
    else:
        raise ValueError("frontmatter is missing its closing '---' delimiter")

    body = "\n".join(lines[closing + 1 :]).strip("\n")
    return frontmatter, body


def _parse_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601 datetime") from exc
    raise TypeError(f"{name} must be a datetime or ISO-8601 string")


def _normalize_content(text: str) -> str:
    return _normalize_line_endings(text).rstrip()


def _safe_filename(title: str, fallback: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", title.strip())
    value = value.strip("._")[:100]
    return value or fallback


def _ensure_within(root: Path, candidate: Path, label: str) -> None:
    """Reject output paths that escape their destination directory."""
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{label} output path escapes its directory: {candidate}")


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, datetime):
        return value.isoformat()
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True, slots=True)
class IngestionResult:
    material: Material
    raw_path: Path
    corpus_path: Path
    used_ocr: bool
    extracted_via: str


class PdfPlumberExtractor:
    """PDF text extraction backed by the optional ``pdfplumber`` package."""

    name = "pdfplumber"

    @staticmethod
    def _import_pdfplumber():
        import pdfplumber

        return pdfplumber

    def extract(self, path: Path) -> str:
        try:
            pdfplumber = self._import_pdfplumber()
        except ImportError as exc:
            raise RuntimeError(
                "pdfplumber is an optional dependency; install it with "
                "'pip install pdfplumber' to enable PDF ingestion"
            ) from exc
        with pdfplumber.open(path) as pdf:
            return "\n\n".join((page.extract_text() or "") for page in pdf.pages).strip()


class IngestionService:
    """Normalize Markdown/PDF inputs into standard corpus documents."""

    def __init__(
        self,
        paths: PathConfig,
        *,
        pdf_extractor: TextExtractor | None = None,
        ocr_extractor: TextExtractor | None = None,
        min_text_chars: int = 80,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths.resolve()
        if min_text_chars < 0:
            raise ValueError("min_text_chars must be non-negative")
        self.pdf_extractor = pdf_extractor or PdfPlumberExtractor()
        self.ocr_extractor = ocr_extractor
        self.min_text_chars = min_text_chars
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def ingest(
        self, path: str | Path, metadata: dict[str, Any] | None = None
    ) -> IngestionResult:
        source_path = Path(path).expanduser()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("metadata must be a mapping")

        suffix = source_path.suffix.lower()
        if suffix in {".md", ".markdown"}:
            with source_path.open("r", encoding="utf-8-sig", newline="") as f:
                existing, body = parse_frontmatter(f.read())
            content = _normalize_content(body)
            extracted_via, used_ocr, original_format = "direct", False, "md"
            merged = {**existing, **(metadata or {})}
        elif suffix == ".pdf":
            content = _normalize_content(self.pdf_extractor.extract(source_path))
            extracted_via, used_ocr, original_format = self.pdf_extractor.name, False, "pdf"
            if len(content) < self.min_text_chars:
                if self.ocr_extractor is None:
                    raise RuntimeError(
                        "OCR extractor is required when PDF text is insufficient"
                    )
                content = _normalize_content(self.ocr_extractor.extract(source_path))
                extracted_via, used_ocr = self.ocr_extractor.name, True
            merged = dict(metadata or {})
        else:
            raise ValueError(f"unsupported input format: {suffix or '<none>'}")
        if not content.strip():
            raise ValueError("extracted content must not be empty")

        title = merged.get("title")
        if not isinstance(title, str) or not title.strip():
            title = source_path.stem
        source = merged.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("metadata.source must be a non-empty string")
        published_at = _parse_datetime(merged.get("published_at"), "published_at")
        fetched_value = merged.get("fetched_at")
        fetched_at = (
            self.clock()
            if fetched_value is None
            else _parse_datetime(fetched_value, "fetched_at")
        )
        material_id = stable_material_id(source, content)

        self.paths.raw_dir.mkdir(parents=True, exist_ok=True)
        self.paths.corpus_dir.mkdir(parents=True, exist_ok=True)
        raw_destination = self.paths.raw_dir / f"{material_id}{suffix}"
        corpus_destination = (
            self.paths.corpus_dir / f"{_safe_filename(title, material_id)}-{material_id}.md"
        )
        _ensure_within(self.paths.raw_dir, raw_destination, "raw")
        _ensure_within(self.paths.corpus_dir, corpus_destination, "corpus")
        shutil.copy2(source_path, raw_destination)

        material = Material(
            id=material_id,
            title=title,
            source=source,
            published_at=published_at,
            fetched_at=fetched_at,
            type=str(merged.get("type") or "unknown"),
            content=content,
            original_format=original_format,
            ocr=used_ocr,
            extracted_via=extracted_via,
            raw_path=raw_destination.as_posix(),
            case_tags=merged.get("case_tags", ()),
            url=merged.get("url"),
        )
        frontmatter = {
            "source_id": material.id,
            "title": material.title,
            "source": material.source,
            "published_at": material.published_at,
            "fetched_at": material.fetched_at,
            "type": material.type,
            "case_tags": material.case_tags,
            "original_format": material.original_format,
            "ocr": material.ocr,
            "extracted_via": material.extracted_via,
            "raw_path": material.raw_path,
            "url": material.url,
        }
        lines = (
            ["---"]
            + [f"{key}: {_yaml_scalar(value)}" for key, value in frontmatter.items()]
            + ["---", "", content, ""]
        )
        with corpus_destination.open("w", encoding="utf-8", newline="\n") as out:
            out.write("\n".join(lines))
        return IngestionResult(
            material, raw_destination, corpus_destination, used_ocr, extracted_via
        )
