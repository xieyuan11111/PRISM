"""SQLite/FTS5 text evidence index for PRISM (module 3).

The store scans the standard corpus of frontmatter-carrying Markdown files,
indexes their metadata and full text into a SQLite database under
``PathConfig.data_dir`` (``index.db``) and exposes FTS5 full-text search with
case tag / source / type / time-range filters.

Design notes:

* The database is a pure index: the corpus Markdown files remain the readable
  source of truth and can always rebuild it (``initialize`` + ``index_directory``).
* Upserts are keyed by ``source_id`` and are idempotent: re-indexing an
  unchanged file is a no-op, while content or metadata changes update the
  single existing record — duplicates are never created.
* FTS5 is used with the ``unicode61`` tokenizer.  Because the corpus is
  primarily Chinese-language news, contiguous CJK runs are expanded into
  character bigrams (on both the indexed text and the query side) so that
  single terms like ``政策`` match inside longer phrases.
* All stored paths are relative to the project root (the directory that
  contains ``corpus_dir``), so the index is portable across machines.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from prism.config import PathConfig
from prism.domain import EvidenceLocator
from prism.domain.models import ORIGINAL_FORMATS
from prism.ingestion import content_hash, parse_frontmatter, stable_material_id

from .models import BatchResult, IndexEntry, IndexOutcome, SearchFilter, SearchHit

DB_FILENAME = "index.db"
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{1,2}")

_FTS_COLUMNS = (
    "source_id",
    "title",
    "source",
    "published_at",
    "fetched_at",
    "type",
    "case_tags",
    "original_format",
    "ocr",
    "extracted_via",
    "raw_path",
    "url",
    "retrieval_level",
    "access_level",
    "doi",
    "pmid",
    "pmcid",
    "authors",
    "container_title",
    "path",
    "content",
)

# Columns added after the original release: ``documents`` gains them through
# ALTER TABLE, and the FTS5 index is rebuilt when it lacks any of them.
_EXTRA_COLUMNS = {
    "retrieval_level": "TEXT",
    "access_level": "TEXT",
    "doi": "TEXT",
    "pmid": "TEXT",
    "pmcid": "TEXT",
    "authors": "TEXT NOT NULL DEFAULT '[]'",
    "container_title": "TEXT",
}

_FTS_TABLE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    source_id UNINDEXED,
    title,
    source UNINDEXED,
    published_at UNINDEXED,
    fetched_at UNINDEXED,
    type UNINDEXED,
    case_tags UNINDEXED,
    original_format UNINDEXED,
    ocr UNINDEXED,
    extracted_via UNINDEXED,
    raw_path UNINDEXED,
    url UNINDEXED,
    retrieval_level UNINDEXED,
    access_level UNINDEXED,
    doi UNINDEXED,
    pmid UNINDEXED,
    pmcid UNINDEXED,
    authors UNINDEXED,
    container_title UNINDEXED,
    path UNINDEXED,
    content,
    tokenize = 'unicode61'
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    source_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    type TEXT NOT NULL,
    case_tags TEXT NOT NULL DEFAULT '[]',
    original_format TEXT,
    ocr INTEGER NOT NULL DEFAULT 0,
    extracted_via TEXT,
    raw_path TEXT,
    url TEXT,
    retrieval_level TEXT,
    access_level TEXT,
    doi TEXT,
    pmid TEXT,
    pmcid TEXT,
    authors TEXT NOT NULL DEFAULT '[]',
    container_title TEXT,
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents(source);
CREATE INDEX IF NOT EXISTS documents_type_idx ON documents(type);
CREATE INDEX IF NOT EXISTS documents_published_at_idx ON documents(published_at);
CREATE INDEX IF NOT EXISTS documents_fetched_at_idx ON documents(fetched_at);
CREATE INDEX IF NOT EXISTS documents_path_idx ON documents(path);
""" + _FTS_TABLE_SQL


def bigramize(text: str) -> str:
    """Expand CJK runs into character bigrams for FTS5 tokenization.

    ``房地产政策`` becomes ``房地 地产 产政 政策`` so that the unicode61
    tokenizer can match shorter queries such as ``政策`` or ``房地产``.
    ASCII text is passed through unchanged.
    """

    def expand(match: re.Match[str]) -> str:
        run = match.group(0)
        if len(run) <= 2:
            return f" {run} "
        return " " + " ".join(run[i : i + 2] for i in range(len(run) - 1)) + " "

    return _CJK_RUN.sub(expand, text)


def _search_terms(query: str) -> tuple[str, ...]:
    """Deduplicated searchable terms of a query, for snippet highlighting."""
    seen: list[str] = []
    for term in _WORD.findall(bigramize(query)):
        if term not in seen:
            seen.append(term)
    return tuple(seen)


def _fts_literal_query(query: str) -> str:
    """Encode user text as literal FTS5 terms, never as an expression."""
    terms = _search_terms(query)
    if not terms:
        raise ValueError("invalid full-text query: query must contain searchable text")
    return " ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def make_snippet(content: str, query: str, width: int = 160) -> str:
    """Return a short window of ``content`` around the first query term."""
    text = re.sub(r"\s+", " ", content).strip()
    if not text:
        return ""
    lower = text.lower()
    index = -1
    for term in _search_terms(query):
        found = lower.find(term.lower())
        if found != -1:
            index = found
            break
    if index == -1:
        index = 0
    start = max(0, index - width // 3)
    end = min(len(text), start + width)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _normalize_line_endings(text: str) -> str:
    return re.sub(r"\r\r\n|\r\n|\r", "\n", text)


def _parse_datetime(value: Any, name: str) -> datetime:
    if value is None:
        raise ValueError(f"{name} is required")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601 datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return parsed.astimezone(timezone.utc)
    raise ValueError(f"{name} must be a datetime or ISO-8601 string")


def _iso_utc(value: datetime) -> str:
    """Normalize an aware datetime to UTC ISO-8601 for stable string compares."""
    return value.astimezone(timezone.utc).isoformat()


def _parse_text_list(name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise ValueError(f"{name} must be a list of non-empty strings")
    try:
        items = tuple(value)
    except TypeError:
        raise ValueError(f"{name} must be a list of non-empty strings") from None
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} must be a list of non-empty strings")
    return items


def _parse_case_tags(value: Any) -> tuple[str, ...]:
    return _parse_text_list("case_tags", value)


def _parse_ocr(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError("ocr must be a boolean")
    return value


def _parse_original_format(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in ORIGINAL_FORMATS:
        allowed = ", ".join(sorted(ORIGINAL_FORMATS))
        raise ValueError(f"original_format must be one of: {allowed}")
    return value


def _parse_optional_text(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _parse_required_text(name: str, value: Any) -> str:
    parsed = _parse_optional_text(name, value)
    if parsed is None:
        raise ValueError(f"frontmatter key {name!r} is required")
    return parsed


class EvidenceStore:
    """SQLite/FTS5 text evidence index backed by the corpus Markdown files."""

    def __init__(
        self,
        paths: PathConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths.resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._connection: sqlite3.Connection | None = None
        self._initialized = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def db_path(self) -> Path:
        """The SQLite database file, always under the configured data dir."""
        return self.paths.data_dir / DB_FILENAME

    def initialize(self) -> None:
        """Create the data directory and the schema (idempotent)."""
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        connection = self._raw_connection()
        connection.executescript(_SCHEMA)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(documents)")
        }
        for name, declaration in _EXTRA_COLUMNS.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE documents ADD COLUMN {name} {declaration}"
                )
        fts_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(document_fts)")
        }
        if not _EXTRA_COLUMNS.keys() <= fts_columns:
            # FTS5 virtual tables cannot be ALTERed in place: rebuild the
            # index from ``documents`` so pre-migration databases keep
            # searchable legacy rows under the new column set.
            with connection:
                connection.execute("DROP TABLE document_fts")
                connection.execute(_FTS_TABLE_SQL)
                self._rebuild_fts(connection)
        self._initialized = True

    @staticmethod
    def _rebuild_fts(connection: sqlite3.Connection) -> None:
        """Repopulate the FTS index from every ``documents`` row."""
        rows = connection.execute(
            "SELECT rowid, source_id, title, source, published_at, fetched_at,"
            " type, case_tags, original_format, ocr, extracted_via, raw_path,"
            " url, retrieval_level, access_level, doi, pmid, pmcid, authors, container_title,"
            " path, content"
            " FROM documents"
        ).fetchall()
        placeholders = ", ".join("?" for _ in range(len(_FTS_COLUMNS) + 1))
        insert = (
            f"INSERT INTO document_fts (rowid, {', '.join(_FTS_COLUMNS)})"
            f" VALUES ({placeholders})"
        )
        for row in rows:
            values = (
                row["source_id"],
                bigramize(row["title"]),
                row["source"],
                row["published_at"],
                row["fetched_at"],
                row["type"],
                row["case_tags"],
                row["original_format"],
                row["ocr"],
                row["extracted_via"],
                row["raw_path"],
                row["url"],
                row["retrieval_level"],
                row["access_level"],
                row["doi"],
                row["pmid"],
                row["pmcid"],
                row["authors"],
                row["container_title"],
                row["path"],
                bigramize(row["content"]),
            )
            connection.execute(insert, (row["rowid"], *values))

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def _raw_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            self._connection = conn
        return self._connection

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._initialized = False

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- indexing ----------------------------------------------------------

    def index_file(self, path: str | Path) -> IndexOutcome:
        """Parse one corpus Markdown file and upsert it into the index."""
        source_path = Path(path).expanduser()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        text = source_path.read_text(encoding="utf-8-sig")
        normalized = _normalize_line_endings(text.lstrip("\ufeff"))
        if not normalized.lstrip().startswith("---"):
            raise ValueError(f"missing frontmatter in {source_path}")

        frontmatter, body = parse_frontmatter(text)
        if not body.strip():
            raise ValueError("document body must not be empty")
        content = body

        source = _parse_required_text("source", frontmatter.get("source"))
        published_at = _parse_datetime(frontmatter.get("published_at"), "published_at")
        fetched_value = frontmatter.get("fetched_at")
        fetched_at = (
            published_at
            if fetched_value is None
            else _parse_datetime(fetched_value, "fetched_at")
        )
        if fetched_at < published_at:
            raise ValueError("fetched_at must not be earlier than published_at")

        source_id = frontmatter.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            source_id = stable_material_id(source, content)
        title = frontmatter.get("title")
        if not isinstance(title, str) or not title.strip():
            title = source_path.stem
        doc_type = frontmatter.get("type")
        if not isinstance(doc_type, str) or not doc_type.strip():
            doc_type = "unknown"

        entry = IndexEntry(
            source_id=source_id.strip(),
            title=title,
            source=source,
            published_at=published_at,
            fetched_at=fetched_at,
            type=doc_type,
            content=content,
            path=self._relative_path(source_path).as_posix(),
            content_hash=content_hash(content),
            case_tags=_parse_case_tags(frontmatter.get("case_tags")),
            original_format=_parse_original_format(frontmatter.get("original_format")),
            ocr=_parse_ocr(frontmatter.get("ocr")),
            extracted_via=_parse_optional_text(
                "extracted_via", frontmatter.get("extracted_via")
            ),
            raw_path=_parse_optional_text("raw_path", frontmatter.get("raw_path")),
            url=_parse_optional_text("url", frontmatter.get("url")),
            retrieval_level=_parse_optional_text(
                "retrieval_level", frontmatter.get("retrieval_level")
            ),
            access_level=_parse_optional_text(
                "access_level", frontmatter.get("access_level")
            ),
            doi=_parse_optional_text("doi", frontmatter.get("doi")),
            pmid=_parse_optional_text("pmid", frontmatter.get("pmid")),
            pmcid=_parse_optional_text("pmcid", frontmatter.get("pmcid")),
            authors=_parse_text_list("authors", frontmatter.get("authors")),
            container_title=_parse_optional_text(
                "container_title", frontmatter.get("container_title")
            ),
        )
        return self._upsert(entry)

    def index_directory(self, directory: str | Path | None = None) -> BatchResult:
        """Scan a directory (default: ``corpus_dir``) and index every Markdown file.

        Files that fail validation are reported in ``errors`` and skipped; a
        failure never affects the records of other files.
        """
        root = (
            Path(directory).expanduser().resolve()
            if directory is not None
            else self.paths.corpus_dir
        )
        if not root.is_dir():
            raise FileNotFoundError(root)

        files = sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in MARKDOWN_SUFFIXES
        )
        indexed = updated = unchanged = 0
        errors: list[tuple[str, str]] = []
        for file in files:
            try:
                outcome = self.index_file(file)
            except (ValueError, OSError) as exc:
                errors.append((str(file), str(exc)))
                continue
            if outcome.status == "indexed":
                indexed += 1
            elif outcome.status == "updated":
                updated += 1
            else:
                unchanged += 1
        return BatchResult(
            total=len(files),
            indexed=indexed,
            updated=updated,
            unchanged=unchanged,
            failed=len(errors),
            errors=tuple(errors),
        )

    def _relative_path(self, source_path: Path) -> Path:
        """Project-relative location of a corpus file (portable, no hardcoding)."""
        root = self.paths.corpus_dir.parent
        try:
            return source_path.resolve().relative_to(root)
        except ValueError:
            raise ValueError(
                f"{source_path} is outside the project root {root}"
            ) from None

    def _fts_values(self, entry: IndexEntry) -> tuple[Any, ...]:
        return (
            entry.source_id,
            bigramize(entry.title),
            entry.source,
            entry.published_at.isoformat(),
            entry.fetched_at.isoformat(),
            entry.type,
            json.dumps(list(entry.case_tags), ensure_ascii=False),
            entry.original_format,
            int(entry.ocr),
            entry.extracted_via,
            entry.raw_path,
            entry.url,
            entry.retrieval_level,
            entry.access_level,
            entry.doi,
            entry.pmid,
            entry.pmcid,
            json.dumps(list(entry.authors), ensure_ascii=False),
            entry.container_title,
            entry.path,
            bigramize(entry.content),
        )

    @staticmethod
    def _same_metadata(row: sqlite3.Row, entry: IndexEntry) -> bool:
        if row["content_hash"] != entry.content_hash:
            return False
        if row["title"] != entry.title:
            return False
        if row["source"] != entry.source:
            return False
        if row["published_at"] != entry.published_at.isoformat():
            return False
        if row["fetched_at"] != entry.fetched_at.isoformat():
            return False
        if row["type"] != entry.type:
            return False
        if row["path"] != entry.path:
            return False
        if tuple(json.loads(row["case_tags"])) != entry.case_tags:
            return False
        if row["original_format"] != entry.original_format:
            return False
        if bool(row["ocr"]) != entry.ocr:
            return False
        if row["extracted_via"] != entry.extracted_via:
            return False
        if row["raw_path"] != entry.raw_path:
            return False
        if row["url"] != entry.url:
            return False
        if row["retrieval_level"] != entry.retrieval_level:
            return False
        if row["access_level"] != entry.access_level:
            return False
        if row["doi"] != entry.doi:
            return False
        if row["pmid"] != entry.pmid:
            return False
        if row["pmcid"] != entry.pmcid:
            return False
        if tuple(json.loads(row["authors"])) != entry.authors:
            return False
        if row["container_title"] != entry.container_title:
            return False
        return True

    def _upsert(self, entry: IndexEntry) -> IndexOutcome:
        """Insert or update a single record atomically; never duplicates."""
        self._ensure_initialized()
        conn = self._raw_connection()
        try:
            with conn:
                row = conn.execute(
                    "SELECT rowid, content_hash, title, source, published_at,"
                    " fetched_at, type, case_tags, original_format, ocr,"
                    " extracted_via, raw_path, url, retrieval_level, access_level,"
                    " doi, pmid, pmcid, authors, container_title, path"
                    " FROM documents WHERE source_id = ?",
                    (entry.source_id,),
                ).fetchone()
                if row is not None and self._same_metadata(row, entry):
                    return IndexOutcome(entry, "unchanged")

                fts_values = self._fts_values(entry)
                placeholders = ", ".join("?" for _ in range(len(_FTS_COLUMNS) + 1))
                fts_columns = ", ".join(_FTS_COLUMNS)
                now = self.clock().isoformat()
                if row is None:
                    cursor = conn.execute(
                        "INSERT INTO documents (source_id, title, source,"
                        " published_at, fetched_at, type, case_tags, original_format,"
                        " ocr, extracted_via, raw_path, url, retrieval_level,"
                        " access_level, doi, pmid, pmcid, authors, container_title, path, content,"
                        " content_hash, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            entry.source_id,
                            entry.title,
                            entry.source,
                            entry.published_at.isoformat(),
                            entry.fetched_at.isoformat(),
                            entry.type,
                            json.dumps(list(entry.case_tags), ensure_ascii=False),
                            entry.original_format,
                            int(entry.ocr),
                            entry.extracted_via,
                            entry.raw_path,
                            entry.url,
                            entry.retrieval_level,
                            entry.access_level,
                            entry.doi,
                            entry.pmid,
                            entry.pmcid,
                            json.dumps(list(entry.authors), ensure_ascii=False),
                            entry.container_title,
                            entry.path,
                            entry.content,
                            entry.content_hash,
                            now,
                        ),
                    )
                    conn.execute(
                        f"INSERT INTO document_fts (rowid, {fts_columns})"
                        f" VALUES ({placeholders})",
                        (cursor.lastrowid, *fts_values),
                    )
                    return IndexOutcome(entry, "indexed")

                conn.execute(
                    "UPDATE documents SET title=?, source=?, published_at=?,"
                    " fetched_at=?, type=?, case_tags=?, original_format=?, ocr=?,"
                    " extracted_via=?, raw_path=?, url=?, retrieval_level=?,"
                    " access_level=?, doi=?, pmid=?, pmcid=?, authors=?, container_title=?,"
                    " path=?, content=?, content_hash=?, updated_at=?"
                    " WHERE source_id=?",
                    (
                        entry.title,
                        entry.source,
                        entry.published_at.isoformat(),
                        entry.fetched_at.isoformat(),
                        entry.type,
                        json.dumps(list(entry.case_tags), ensure_ascii=False),
                        entry.original_format,
                        int(entry.ocr),
                        entry.extracted_via,
                        entry.raw_path,
                        entry.url,
                        entry.retrieval_level,
                        entry.access_level,
                        entry.doi,
                        entry.pmid,
                        entry.pmcid,
                        json.dumps(list(entry.authors), ensure_ascii=False),
                        entry.container_title,
                        entry.path,
                        entry.content,
                        entry.content_hash,
                        now,
                        entry.source_id,
                    ),
                )
                conn.execute("DELETE FROM document_fts WHERE rowid = ?", (row["rowid"],))
                conn.execute(
                    f"INSERT INTO document_fts (rowid, {fts_columns})"
                    f" VALUES ({placeholders})",
                    (row["rowid"], *fts_values),
                )
                return IndexOutcome(entry, "updated")
        except sqlite3.Error as exc:
            raise RuntimeError(f"failed to index {entry.source_id}: {exc}") from exc

    # -- queries -----------------------------------------------------------

    def get(self, source_id: str) -> IndexEntry | None:
        """Return the indexed entry for ``source_id``, or ``None``."""
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        self._ensure_initialized()
        row = self._raw_connection().execute(
            "SELECT source_id, title, source, published_at, fetched_at, type,"
            " case_tags, original_format, ocr, extracted_via, raw_path, url,"
            " retrieval_level, access_level, doi, pmid, pmcid, authors, container_title, path,"
            " content, content_hash FROM documents WHERE source_id = ?",
            (source_id.strip(),),
        ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    def locate(
        self,
        source_id: str,
        *,
        quote: str | None = None,
        paragraph: int | None = None,
        page: int | None = None,
        max_quote_chars: int = 360,
    ) -> EvidenceLocator:
        """Resolve a source to a portable corpus paragraph/page and excerpt.

        Paragraphs are one-based non-empty logical lines in normalized corpus
        Markdown.  When ``quote`` is supplied it must occur in the selected
        paragraph (ignoring whitespace differences); failure is explicit so a
        caller cannot accidentally cite text absent from the source.
        """

        if isinstance(max_quote_chars, bool) or not isinstance(max_quote_chars, int):
            raise TypeError("max_quote_chars must be an integer")
        if max_quote_chars < 40:
            raise ValueError("max_quote_chars must be at least 40")
        entry = self.get(source_id)
        if entry is None:
            raise LookupError(f"material not found: {source_id}")
        paragraphs = tuple(
            line.strip() for line in entry.content.splitlines() if line.strip()
        )
        if not paragraphs:
            raise LookupError(f"material has no locatable text: {source_id}")
        if paragraph is not None:
            if isinstance(paragraph, bool) or not isinstance(paragraph, int) or paragraph < 1:
                raise ValueError("paragraph must be a positive integer")
            if paragraph > len(paragraphs):
                raise LookupError(
                    f"paragraph {paragraph} is outside material {source_id}"
                )
            candidates = ((paragraph, paragraphs[paragraph - 1]),)
        else:
            candidates = tuple(enumerate(paragraphs, start=1))

        requested = None
        if quote is not None:
            if not isinstance(quote, str) or not quote.strip():
                raise ValueError("quote must be a non-empty string")
            requested = re.sub(r"\s+", "", quote)
            candidates = tuple(
                item for item in candidates if requested in re.sub(r"\s+", "", item[1])
            )
            if not candidates:
                if paragraph is not None:
                    raise LookupError(
                        f"quote was not found in paragraph {paragraph} of "
                        f"material {source_id}"
                    )
                raise LookupError(f"quote was not found in material {source_id}")

        number, text = candidates[0]
        excerpt = text
        if len(excerpt) > max_quote_chars:
            if requested is None:
                excerpt = excerpt[: max_quote_chars - 1].rstrip() + "…"
            else:
                compact = re.sub(r"\s+", "", excerpt)
                start = max(0, compact.find(requested) - max_quote_chars // 4)
                excerpt = compact[start : start + max_quote_chars]
                if start:
                    excerpt = "…" + excerpt
                if start + max_quote_chars < len(compact):
                    excerpt += "…"
        return EvidenceLocator(
            source_id=entry.source_id,
            corpus_path=entry.path,
            paragraph=number,
            page=page,
            quote=excerpt,
        )

    def search(
        self,
        criteria: SearchFilter | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SearchHit]:
        """Full-text search with metadata/time-range filters, stably ordered."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if criteria is None:
            criteria = SearchFilter()
        if not isinstance(criteria, SearchFilter):
            raise TypeError("criteria must be a SearchFilter")
        self._ensure_initialized()

        where, params = self._where_clause(criteria)
        if criteria.query is not None:
            sql = (
                "SELECT d.source_id, d.title, d.source, d.published_at, d.type,"
                " d.case_tags, d.raw_path, d.url, d.retrieval_level,"
                " d.access_level, d.doi, d.pmid, d.pmcid, d.authors, d.container_title,"
                " d.path, d.content"
                " FROM document_fts JOIN documents AS d"
                " ON d.rowid = document_fts.rowid"
                f" WHERE {where}"
                " ORDER BY document_fts.rank, d.source_id LIMIT ? OFFSET ?"
            )
        else:
            sql = (
                "SELECT d.source_id, d.title, d.source, d.published_at, d.type,"
                " d.case_tags, d.raw_path, d.url, d.retrieval_level,"
                " d.access_level, d.doi, d.pmid, d.pmcid, d.authors, d.container_title,"
                " d.path, d.content"
                " FROM documents AS d"
                f" WHERE {where}"
                " ORDER BY d.published_at DESC, d.source_id LIMIT ? OFFSET ?"
            )
        try:
            rows = self._raw_connection().execute(sql, (*params, limit, offset)).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"invalid full-text query: {exc}") from exc
        return [self._row_to_hit(row, criteria.query or "") for row in rows]

    def delete(self, source_id: str) -> bool:
        """Remove a record and its FTS entry; returns whether it existed."""
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        self._ensure_initialized()
        conn = self._raw_connection()
        row = conn.execute(
            "SELECT rowid FROM documents WHERE source_id = ?", (source_id.strip(),)
        ).fetchone()
        if row is None:
            return False
        with conn:
            conn.execute(
                "DELETE FROM documents WHERE source_id = ?", (source_id.strip(),)
            )
            conn.execute("DELETE FROM document_fts WHERE rowid = ?", (row["rowid"],))
        return True

    def _where_clause(self, criteria: SearchFilter) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if criteria.query is not None:
            clauses.append("document_fts MATCH ?")
            params.append(_fts_literal_query(criteria.query))
        if criteria.case_tag is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(d.case_tags)"
                " WHERE json_each.value = ?)"
            )
            params.append(criteria.case_tag)
        if criteria.source is not None:
            clauses.append("d.source = ?")
            params.append(criteria.source)
        if criteria.type is not None:
            clauses.append("d.type = ?")
            params.append(criteria.type)
        if criteria.published_after is not None:
            clauses.append("d.published_at >= ?")
            params.append(_iso_utc(criteria.published_after))
        if criteria.published_before is not None:
            clauses.append("d.published_at <= ?")
            params.append(_iso_utc(criteria.published_before))
        where = " AND ".join(clauses) if clauses else "1 = 1"
        return where, params

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> IndexEntry:
        return IndexEntry(
            source_id=row["source_id"],
            title=row["title"],
            source=row["source"],
            published_at=datetime.fromisoformat(row["published_at"]),
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            type=row["type"],
            content=row["content"],
            path=row["path"],
            content_hash=row["content_hash"],
            case_tags=tuple(json.loads(row["case_tags"])),
            original_format=row["original_format"],
            ocr=bool(row["ocr"]),
            extracted_via=row["extracted_via"],
            raw_path=row["raw_path"],
            url=row["url"],
            retrieval_level=row["retrieval_level"],
            access_level=row["access_level"],
            doi=row["doi"],
            pmid=row["pmid"],
            pmcid=row["pmcid"],
            authors=tuple(json.loads(row["authors"])),
            container_title=row["container_title"],
        )

    @staticmethod
    def _row_to_hit(row: sqlite3.Row, query: str) -> SearchHit:
        return SearchHit(
            source_id=row["source_id"],
            title=row["title"],
            path=row["path"],
            snippet=make_snippet(row["content"], query),
            source=row["source"],
            type=row["type"],
            published_at=datetime.fromisoformat(row["published_at"]),
            case_tags=tuple(json.loads(row["case_tags"])),
            url=row["url"],
            raw_path=row["raw_path"],
            retrieval_level=row["retrieval_level"],
            access_level=row["access_level"],
            doi=row["doi"],
            pmid=row["pmid"],
            pmcid=row["pmcid"],
            authors=tuple(json.loads(row["authors"])),
            container_title=row["container_title"],
        )
