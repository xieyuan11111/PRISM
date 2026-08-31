"""Public ingestion API."""
from .service import (
    IngestionResult,
    IngestionService,
    PdfPlumberExtractor,
    content_hash,
    parse_frontmatter,
    stable_material_id,
)

__all__ = [
    "IngestionResult",
    "IngestionService",
    "PdfPlumberExtractor",
    "content_hash",
    "parse_frontmatter",
    "stable_material_id",
]
