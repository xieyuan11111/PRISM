from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.config import PathConfig
from prism.ingestion import (
    IngestionService,
    PdfPlumberExtractor,
    content_hash,
    parse_frontmatter,
    stable_material_id,
)


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


class FakePdfExtractor:
    name = "fake-pdf"

    def __init__(self, text: str):
        self.text = text

    def extract(self, path: Path) -> str:
        return self.text


class FakeOcrExtractor:
    name = "fake-ocr"

    def __init__(self, text: str):
        self.text = text
        self.calls = []

    def extract(self, path: Path) -> str:
        self.calls.append(path)
        return self.text


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        raw_dir=Path("raw"),
        corpus_dir=Path("corpus"),
    )


def metadata():
    return {
        "title": "A policy",
        "source": "example.gov",
        "published_at": NOW,
        "fetched_at": NOW,
        "type": "policy",
        "case_tags": ["housing"],
    }


def test_markdown_is_normalized_and_original_is_retained(tmp_path):
    source = tmp_path / "input.md"
    source.write_text("# A policy\n\nContent", encoding="utf-8")
    service = IngestionService(make_paths(tmp_path))

    result = service.ingest(source, metadata())

    assert result.used_ocr is False
    assert result.material.original_format == "md"
    assert result.material.extracted_via == "direct"
    assert result.raw_path.exists()
    assert result.corpus_path.exists()
    assert result.material.content == "# A policy\n\nContent"
    assert result.corpus_path.read_text(encoding="utf-8").startswith("---\n")


def test_pdf_uses_text_extractor_when_text_is_sufficient(tmp_path):
    source = tmp_path / "policy.pdf"
    source.write_bytes(b"fake pdf")
    pdf = FakePdfExtractor("A sufficiently long extracted policy text.\n" * 4)
    ocr = FakeOcrExtractor("OCR text")
    service = IngestionService(make_paths(tmp_path), pdf_extractor=pdf, ocr_extractor=ocr, min_text_chars=40)

    result = service.ingest(source, metadata())

    assert result.used_ocr is False
    assert result.material.extracted_via == "fake-pdf"
    assert not ocr.calls
    assert "sufficiently long" in result.material.content


def test_pdf_falls_back_to_ocr_when_text_is_too_short(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake pdf")
    ocr = FakeOcrExtractor("OCR recovered policy text.\n" * 4)
    service = IngestionService(
        make_paths(tmp_path),
        pdf_extractor=FakePdfExtractor("tiny"),
        ocr_extractor=ocr,
        min_text_chars=40,
    )

    result = service.ingest(source, metadata())

    assert result.used_ocr is True
    assert result.material.ocr is True
    assert result.material.extracted_via == "fake-ocr"
    assert ocr.calls == [source]


def test_pdf_without_ocr_extractor_fails_clearly(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake pdf")
    service = IngestionService(
        make_paths(tmp_path),
        pdf_extractor=FakePdfExtractor("tiny"),
        min_text_chars=40,
    )

    with pytest.raises(RuntimeError, match="OCR extractor"):
        service.ingest(source, metadata())


def test_stable_material_id_is_deterministic_and_content_sensitive():
    first = stable_material_id("example.gov", "same body")
    second = stable_material_id("example.gov", "same body")
    changed = stable_material_id("example.gov", "changed body")

    assert first == second
    assert first != changed
    assert first.startswith("mat_")


def test_output_filename_cannot_escape_corpus(tmp_path):
    source = tmp_path / "input.md"
    source.write_text("body", encoding="utf-8")
    service = IngestionService(make_paths(tmp_path))

    result = service.ingest(source, {**metadata(), "title": "../../outside"})

    assert result.corpus_path.is_relative_to((tmp_path / "corpus").resolve())


def test_existing_markdown_frontmatter_is_validated_and_completed(tmp_path):
    source = tmp_path / "input.md"
    source.write_text(
        "---\r\n"
        "title: Existing title\r\n"
        "source: example.gov\r\n"
        "published_at: 2026-08-31T12:00:00+00:00\r\n"
        "type: policy\r\n"
        "case_tags:\r\n"
        "  - housing\r\n"
        "---\r\n\r\n"
        "# Existing title\r\n\r\nContent\r\n",
        encoding="utf-8",
    )
    service = IngestionService(make_paths(tmp_path), clock=lambda: NOW)

    result = service.ingest(source)

    frontmatter, body = parse_frontmatter(result.corpus_path.read_text(encoding="utf-8"))
    assert frontmatter["source_id"] == result.material.id
    assert frontmatter["fetched_at"] == NOW.isoformat()
    assert frontmatter["original_format"] == "md"
    assert frontmatter["ocr"] is False
    assert frontmatter["extracted_via"] == "direct"
    assert frontmatter["case_tags"] == ["housing"]
    assert body == "# Existing title\n\nContent"


def test_invalid_frontmatter_date_is_rejected(tmp_path):
    source = tmp_path / "invalid.md"
    source.write_text(
        "---\ntitle: Invalid\nsource: example.gov\n"
        "published_at: not-a-date\ntype: policy\n---\nBody",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="published_at"):
        IngestionService(make_paths(tmp_path)).ingest(source)


def test_content_hash_normalizes_line_endings():
    assert content_hash("same\r\nbody") == content_hash("same\nbody")


def test_relative_ingestion_paths_use_prism_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path))
    source = tmp_path / "input.md"
    source.write_text("body", encoding="utf-8")
    paths = PathConfig(raw_dir="raw", corpus_dir="corpus")

    result = IngestionService(paths).ingest(source, metadata())

    assert result.raw_path.is_relative_to(tmp_path / "raw")
    assert result.corpus_path.is_relative_to(tmp_path / "corpus")


def test_pdfplumber_missing_dependency_has_actionable_error(tmp_path, monkeypatch):
    source = tmp_path / "input.pdf"
    source.write_bytes(b"fake pdf")

    def missing_pdfplumber():
        raise ModuleNotFoundError("No module named 'pdfplumber'")

    monkeypatch.setattr(PdfPlumberExtractor, "_import_pdfplumber", staticmethod(missing_pdfplumber))

    with pytest.raises(RuntimeError, match=r"optional dependency.*pdfplumber"):
        PdfPlumberExtractor().extract(source)
