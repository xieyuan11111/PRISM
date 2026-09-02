"""Dependency-free academic metadata fallback clients.

The clients only consume injected public HTTP responses.  They deliberately
return metadata records and ``SourceItem`` objects with ``content=None``:
an abstract is evidence about a work, not the work's full text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

from .http import HttpGetter, HttpResponse
from .models import FailureKind, SourceFetchError, SourceItem

_ACCESS_LEVELS = frozenset({"fulltext", "abstract_only", "metadata_only", "blocked"})
_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_DOI_PREFIX_PATTERN = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_TRAILING_DOI_PUNCTUATION = ".,;:!?)]}>\"'"
_API_HOSTS = frozenset({"api.crossref.org", "api.openalex.org"})


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return " ".join(value.split())


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    normalized = " ".join(value.split())
    return normalized or None


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _valid_http_url(value: object, name: str = "url") -> str:
    text = _text(value, name)
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{name} must not contain credentials")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{name} contains control characters")
    return text


def _normalize_candidate(candidate: str) -> str | None:
    candidate = unquote(candidate.strip())
    candidate = _DOI_PREFIX_PATTERN.sub("", candidate).strip()
    candidate = candidate.split("?", 1)[0].split("#", 1)[0]
    candidate = candidate.rstrip(_TRAILING_DOI_PUNCTUATION)
    match = _DOI_PATTERN.fullmatch(candidate)
    if match is None:
        return None
    return candidate.lower()


def normalize_doi(value: str) -> str:
    """Validate and canonicalize a DOI to its lowercase bare form."""
    if not isinstance(value, str):
        raise TypeError("doi must be a string")
    normalized = _normalize_candidate(value)
    if normalized is None:
        raise ValueError("invalid DOI")
    return normalized


def extract_doi(value: str) -> str | None:
    """Extract a DOI from a DOI URL, DOI string, or article URL."""
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    decoded = unquote(value.strip())
    prefix_match = _DOI_PREFIX_PATTERN.match(decoded)
    if prefix_match:
        return _normalize_candidate(decoded)
    match = _DOI_PATTERN.search(decoded)
    if match is None:
        return None
    return _normalize_candidate(match.group(0))


class MetadataClient(Protocol):
    async def fetch(self, doi: str, *, retrieved_at: datetime) -> "AcademicRecord": ...


@dataclass(frozen=True, slots=True)
class AcademicRecord:
    """Validated, source-neutral academic metadata."""

    title: str
    source: str
    link: str
    retrieved_at: datetime
    published_at: datetime | None
    authors: tuple[str, ...]
    doi: str
    abstract: str | None
    access_level: str
    container_title: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "link", _valid_http_url(self.link, "link"))
        object.__setattr__(self, "retrieved_at", _aware(self.retrieved_at, "retrieved_at"))
        if self.published_at is not None:
            published = _aware(self.published_at, "published_at")
            if published > self.retrieved_at:
                raise ValueError("published_at must not be later than retrieved_at")
            object.__setattr__(self, "published_at", published)
        if isinstance(self.authors, str):
            raise TypeError("authors must be an iterable of strings")
        authors = tuple(self.authors)
        for author in authors:
            _text(author, "author")
        object.__setattr__(self, "authors", authors)
        object.__setattr__(self, "doi", normalize_doi(self.doi))
        object.__setattr__(self, "abstract", _optional_text(self.abstract, "abstract"))
        object.__setattr__(self, "container_title", _optional_text(self.container_title, "container_title"))
        if self.access_level not in _ACCESS_LEVELS:
            raise ValueError(f"access_level must be one of {sorted(_ACCESS_LEVELS)}")
        if self.access_level == "abstract_only" and self.abstract is None:
            raise ValueError("abstract_only records require an abstract")
        if self.access_level == "metadata_only" and self.abstract is not None:
            raise ValueError("metadata_only records must not contain an abstract")

    def to_source_item(self) -> SourceItem:
        """Convert metadata to an honest source item (never full text).

        The bibliographic identity — ``authors``, ``container_title`` and
        ``doi`` — is preserved so ingestion can keep it in the corpus
        frontmatter; only the body stays ``None``.
        """
        return SourceItem(
            title=self.title,
            source="academic",
            fetched_at=self.retrieved_at,
            link=self.link,
            published_at=self.published_at,
            summary=self.abstract,
            content=None,
            type="academic",
            access_level=self.access_level,
            retrieval_level=self.access_level,
            doi=self.doi,
            authors=self.authors,
            container_title=self.container_title,
        )


ScholarlyRecord = AcademicRecord


class _JatsTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_jats(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("abstract must be a string or null")
    parser = _JatsTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception as error:
        raise ValueError("abstract contains malformed markup") from error
    normalized = " ".join("".join(parser.parts).split())
    return normalized or None


def _parse_date_parts(value: object, name: str) -> datetime:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
        raise ValueError(f"{name}.date-parts must contain a date list")
    first = parts[0]
    if not 1 <= len(first) <= 3 or any(isinstance(part, bool) or not isinstance(part, int) for part in first):
        raise ValueError(f"{name}.date-parts is invalid")
    try:
        if len(first) == 1:
            return datetime(first[0], 1, 1, tzinfo=timezone.utc)
        if len(first) == 2:
            return datetime(first[0], first[1], 1, tzinfo=timezone.utc)
        return datetime(*first, tzinfo=timezone.utc)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name}.date-parts is not a valid date") from error


def _parse_iso_date(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} is not a valid ISO date") from error
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def _first_optional(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    return _optional_text(value[0], "container_title")


def _first_title(payload: Mapping[str, object]) -> str:
    values = payload.get("title")
    if not isinstance(values, list) or not values or not isinstance(values[0], str) or not values[0].strip():
        raise ValueError("title must be a non-empty list of strings")
    return values[0]


def _crossref_authors(payload: Mapping[str, object]) -> tuple[str, ...]:
    values = payload.get("author", [])
    if not isinstance(values, list):
        raise ValueError("author must be a list")
    result: list[str] = []
    for entry in values:
        if not isinstance(entry, Mapping):
            raise ValueError("author entries must be objects")
        name = entry.get("name")
        if name is None:
            given, family = entry.get("given"), entry.get("family")
            if family is not None and given is not None:
                name = f"{given} {family}"
            elif family is not None:
                name = family
        if name is not None:
            result.append(_text(name, "author name"))
    return tuple(result)


def _openalex_authors(payload: Mapping[str, object]) -> tuple[str, ...]:
    values = payload.get("authorships", [])
    if not isinstance(values, list):
        raise ValueError("authorships must be a list")
    result: list[str] = []
    for entry in values:
        if not isinstance(entry, Mapping):
            raise ValueError("authorship entries must be objects")
        author = entry.get("author")
        if not isinstance(author, Mapping) or author.get("display_name") is None:
            raise ValueError("authorship author must contain display_name")
        result.append(_text(author["display_name"], "author name"))
    return tuple(result)


def _reconstruct_abstract(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("abstract_inverted_index must be an object")
    words: dict[int, str] = {}
    for word, positions in value.items():
        if not isinstance(word, str) or not word.strip() or not isinstance(positions, list):
            raise ValueError("abstract_inverted_index has invalid entries")
        for position in positions:
            if isinstance(position, bool) or not isinstance(position, int) or position < 0 or position in words:
                raise ValueError("abstract_inverted_index has invalid positions")
            words[position] = word
    return " ".join(words[position] for position in sorted(words)) or None


def _record_link(payload: Mapping[str, object], doi: str) -> str:
    value = payload.get("URL")
    if value is None:
        return f"https://doi.org/{quote(doi, safe='')}"
    return _valid_http_url(value, "URL")


def _openalex_link(payload: Mapping[str, object], doi: str) -> str:
    ids = payload.get("ids")
    if ids is not None and not isinstance(ids, Mapping):
        raise ValueError("ids must be an object")
    if isinstance(ids, Mapping) and ids.get("doi") is not None:
        return _valid_http_url(ids["doi"], "ids.doi")
    location = payload.get("primary_location")
    if location is not None and not isinstance(location, Mapping):
        raise ValueError("primary_location must be an object")
    if isinstance(location, Mapping) and location.get("landing_page_url") is not None:
        return _valid_http_url(location["landing_page_url"], "landing_page_url")
    return f"https://doi.org/{quote(doi, safe='')}"


def _openalex_venue(payload: Mapping[str, object]) -> str | None:
    """Read the venue (journal-like source) OpenAlex assigns to a work.

    OpenAlex has no ``container-title`` field; the closest equivalent is the
    ``display_name`` of the ``source`` of the work's ``primary_location``.
    It is optional metadata: it may be absent, and it only ever fills a
    venue that the preferred provider (Crossref) did not supply.
    """
    location = payload.get("primary_location")
    if location is not None and not isinstance(location, Mapping):
        raise ValueError("primary_location must be an object")
    if not isinstance(location, Mapping):
        return None
    source = location.get("source")
    if source is not None and not isinstance(source, Mapping):
        raise ValueError("primary_location.source must be an object")
    if not isinstance(source, Mapping):
        return None
    return _optional_text(source.get("display_name"), "primary_location.source.display_name")


class _ApiClient:
    endpoint_host: str

    def __init__(self, getter: HttpGetter, *, timeout: float = 10.0, max_response_bytes: int = 2 * 1024 * 1024) -> None:
        if not callable(getattr(getter, "get", None)):
            raise TypeError("getter must provide async get()")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer")
        self._getter = getter
        self._timeout = float(timeout)
        self._max_response_bytes = max_response_bytes

    async def _json(self, url: str) -> Mapping[str, object]:
        try:
            response = await self._getter.get(url, timeout=self._timeout)
        except TimeoutError as error:
            raise SourceFetchError(FailureKind.TIMEOUT, url, "metadata request timed out") from error
        except SourceFetchError as error:
            raise SourceFetchError(
                error.kind,
                url,
                "metadata source request failed",
            ) from None
        except Exception as error:
            raise SourceFetchError(FailureKind.TRANSPORT, url, f"metadata transport failed ({type(error).__name__})") from error
        if not isinstance(response, HttpResponse):
            raise SourceFetchError(FailureKind.TRANSPORT, url, "getter returned an invalid response")
        try:
            _valid_http_url(response.url, "response URL")
        except (TypeError, ValueError) as error:
            raise SourceFetchError(FailureKind.PARSE, url, "response URL is invalid") from error
        if urlsplit(response.url).hostname.lower() not in _API_HOSTS:
            raise SourceFetchError(FailureKind.BLOCKED, url, "metadata response host is not approved")
        if not 200 <= response.status < 300:
            raise SourceFetchError(FailureKind.HTTP_STATUS, url, f"metadata HTTP status {response.status}")
        if len(response.body.encode("utf-8")) > self._max_response_bytes:
            raise SourceFetchError(FailureKind.PARSE, url, "metadata response exceeds size limit")
        try:
            decoded = json.loads(response.body)
        except (TypeError, ValueError) as error:
            raise SourceFetchError(FailureKind.PARSE, url, "metadata response is not valid JSON") from error
        if not isinstance(decoded, Mapping):
            raise SourceFetchError(FailureKind.PARSE, url, "metadata JSON root must be an object")
        return decoded


class CrossrefClient(_ApiClient):
    """Read public Crossref ``works`` metadata without credentials."""

    endpoint_host = "api.crossref.org"

    async def fetch(self, doi: str, *, retrieved_at: datetime) -> AcademicRecord:
        retrieved_at = _aware(retrieved_at, "retrieved_at")
        normalized_doi = normalize_doi(doi)
        url = f"https://api.crossref.org/works/{quote(normalized_doi, safe='')}"
        root = await self._json(url)
        message = root.get("message")
        if not isinstance(message, Mapping):
            raise SourceFetchError(FailureKind.PARSE, url, "Crossref message must be an object")
        try:
            title = _first_title(message)
            raw_doi = message.get("DOI", normalized_doi)
            result_doi = normalize_doi(raw_doi)
            published = None
            for key in ("published", "published-print", "published-online"):
                if key in message:
                    published = _parse_date_parts(message[key], key)
                    break
            abstract = _strip_jats(message.get("abstract"))
            access = "abstract_only" if abstract is not None else "metadata_only"
            return AcademicRecord(
                title=title,
                source="academic",
                link=_record_link(message, result_doi),
                retrieved_at=retrieved_at,
                published_at=published,
                authors=_crossref_authors(message),
                doi=result_doi,
                abstract=abstract,
                access_level=access,
                container_title=_first_optional(message.get("container-title")),
            )
        except (TypeError, ValueError, KeyError) as error:
            raise SourceFetchError(FailureKind.PARSE, url, "Crossref metadata schema is invalid") from error


class OpenAlexClient(_ApiClient):
    """Read public OpenAlex work metadata without credentials."""

    endpoint_host = "api.openalex.org"

    async def fetch(self, doi: str, *, retrieved_at: datetime) -> AcademicRecord:
        retrieved_at = _aware(retrieved_at, "retrieved_at")
        normalized_doi = normalize_doi(doi)
        url = f"https://api.openalex.org/works/https://doi.org/{quote(normalized_doi, safe='')}"
        root = await self._json(url)
        try:
            title = _text(root.get("title"), "title")
            ids = root.get("ids")
            result_doi = normalized_doi
            if isinstance(ids, Mapping) and ids.get("doi") is not None:
                extracted = extract_doi(ids["doi"])
                if extracted is None:
                    raise ValueError("ids.doi is invalid")
                result_doi = extracted
            published = _parse_iso_date(root["publication_date"], "publication_date") if root.get("publication_date") is not None else None
            abstract = _reconstruct_abstract(root.get("abstract_inverted_index"))
            return AcademicRecord(
                title=title,
                source="academic",
                link=_openalex_link(root, result_doi),
                retrieved_at=retrieved_at,
                published_at=published,
                authors=_openalex_authors(root),
                doi=result_doi,
                abstract=abstract,
                access_level="abstract_only" if abstract is not None else "metadata_only",
                container_title=_openalex_venue(root),
            )
        except (TypeError, ValueError, KeyError) as error:
            raise SourceFetchError(FailureKind.PARSE, url, "OpenAlex metadata schema is invalid") from error


def _merge_openalex_abstract(record: AcademicRecord, enriched: AcademicRecord) -> AcademicRecord:
    """Upgrade a Crossref ``metadata_only`` record with an OpenAlex abstract.

    Crossref stays the bibliographic source of truth: its ``title``, ``doi``,
    ``authors``, ``container_title``, ``link`` and ``published_at`` win
    whenever they are present, so an OpenAlex enrichment never discards a
    richer Crossref bibliography.  OpenAlex only contributes the abstract
    (``enriched`` must carry one) plus the fields the Crossref record lacks —
    e.g. authors, the venue from ``primary_location.source.display_name``, or
    a publication date — turning a bare Crossref placeholder into an
    ``abstract_only`` record that still carries the Crossref identity.
    """
    return AcademicRecord(
        title=record.title,
        source="academic",
        link=record.link,
        retrieved_at=record.retrieved_at,
        published_at=(
            record.published_at if record.published_at is not None else enriched.published_at
        ),
        authors=record.authors if record.authors else enriched.authors,
        doi=record.doi,
        abstract=enriched.abstract,
        access_level="abstract_only",
        container_title=(
            record.container_title
            if record.container_title is not None
            else enriched.container_title
        ),
    )


class ScholarlyMetadataClient:
    """Crossref-first DOI resolver with OpenAlex fallback and enrichment."""

    def __init__(self, crossref: MetadataClient, openalex: MetadataClient, *, clock) -> None:
        if not callable(getattr(crossref, "fetch", None)) or not callable(getattr(openalex, "fetch", None)):
            raise TypeError("metadata clients must provide fetch()")
        self._crossref = crossref
        self._openalex = openalex
        self._clock = clock

    async def fetch(self, value: str) -> SourceItem:
        doi = extract_doi(value)
        if doi is None:
            raise SourceFetchError(FailureKind.PARSE, value, "no DOI found in scholarly URL")
        retrieved_at = _aware(self._clock(), "clock result")
        # Crossref is the primary source and is queried at most once per DOI.
        # OpenAlex is consulted at most once per DOI, and only for one of two
        # reasons: Crossref failed (fallback), or Crossref answered without an
        # abstract (enrichment).  A failed fallback re-raises the original
        # Crossref error so its classification (kind, url, detail) survives.
        crossref_answered = False
        try:
            record = await self._crossref.fetch(doi, retrieved_at=retrieved_at)
            crossref_answered = True
        except SourceFetchError as crossref_error:
            try:
                record = await self._openalex.fetch(doi, retrieved_at=retrieved_at)
            except SourceFetchError:
                raise crossref_error
        if crossref_answered and record.abstract is None:
            # Enrichment merges, it never replaces: the OpenAlex abstract is
            # folded into the Crossref record, whose title/doi/authors/
            # container_title/link/published stay authoritative.  A failed
            # enrichment or a bare OpenAlex record (no abstract, so nothing
            # to add) leaves the Crossref record untouched.
            try:
                enriched = await self._openalex.fetch(doi, retrieved_at=retrieved_at)
            except SourceFetchError:
                pass
            else:
                if enriched.abstract is not None:
                    record = _merge_openalex_abstract(record, enriched)
        return record.to_source_item()


__all__ = [
    "AcademicRecord",
    "CrossrefClient",
    "MetadataClient",
    "OpenAlexClient",
    "ScholarlyMetadataClient",
    "ScholarlyRecord",
    "extract_doi",
    "normalize_doi",
]
