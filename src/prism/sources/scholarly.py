"""Dependency-free academic metadata fallback clients.

The clients only consume injected public HTTP responses.  They deliberately
return metadata records and ``SourceItem`` objects with ``content=None``:
an abstract is evidence about a work, not the work's full text.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .http import HttpGetter, HttpResponse
from .models import FailureKind, SourceFetchError, SourceItem

_ACCESS_LEVELS = frozenset({"fulltext", "abstract_only", "metadata_only", "blocked"})
_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_DOI_PREFIX_PATTERN = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_TRAILING_DOI_PUNCTUATION = ".,;:!?)]}>\"'"
_PMID_DIGITS = r"\d{1,9}"
_PMID_VALUE_PATTERN = re.compile(rf"^{_PMID_DIGITS}$")
_PMCID_VALUE_PATTERN = re.compile(r"^PMC\d{1,9}$", re.IGNORECASE)
# Identifier literals: the entire value must be the labeled identifier.  A
# PMID/PMCID is never read out of arbitrary prose or from the query, path,
# or userinfo of a URL on another host.
_PMID_LITERAL_PATTERN = re.compile(rf"^PMID[:\s-]*({_PMID_DIGITS})$", re.IGNORECASE)
_PMCID_LITERAL_PATTERN = re.compile(r"^PMCID[:\s-]*(PMC\d{1,9})$", re.IGNORECASE)
_PMCID_PREFIX_STRIP = re.compile(r"^pmcid[:\s-]*", re.IGNORECASE)
# Genuine PubMed / PubMed Central article URLs only.  Hostnames are matched
# exactly (never as substrings of another host) and each path is anchored so
# the identifier must be the whole final path segment, never an embedded
# fragment of a foreign URL.
_PUBMED_PAGE_HOSTS = frozenset({"pubmed.ncbi.nlm.nih.gov"})
_NCBI_PUBMED_HOSTS = frozenset({"www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"})
_PMC_PAGE_HOSTS = frozenset({"pmc.ncbi.nlm.nih.gov"})
_NCBI_PMC_HOSTS = frozenset({"www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"})
_PMID_ARTICLE_PATH = re.compile(rf"^/({_PMID_DIGITS})/?$")
_PUBMED_INDEX_PATH = re.compile(rf"^/pubmed/({_PMID_DIGITS})/?$")
_PMCID_ARTICLE_PATH = re.compile(r"^/articles/(PMC\d{1,9})/?$", re.IGNORECASE)
_PMC_INDEX_PATH = re.compile(r"^/pmc/articles/(PMC\d{1,9})/?$", re.IGNORECASE)
# Credential-like query keys whose values must never be echoed into an
# exception or audit trail (longer, more specific keys sort first).
_SENSITIVE_QUERY_KEYS = (
    "access_token",
    "client_secret",
    "refresh_token",
    "x-amz-credential",
    "x-amz-signature",
    "api_key",
    "authorization",
    "apikey",
    "credential",
    "password",
    "passwd",
    "secret",
    "signature",
    "token",
    "auth",
    "key",
)
_SENSITIVE_QUERY_VALUE_PATTERN = re.compile(
    r"([&;]|^)(" + "|".join(re.escape(key) for key in _SENSITIVE_QUERY_KEYS) + r")(=)[^&;]*",
    re.IGNORECASE,
)
_API_HOSTS = frozenset({"api.crossref.org", "api.openalex.org", "www.ebi.ac.uk"})
# Bibliographic title search must never guess: a Crossref/OpenAlex hit is
# only accepted when its normalized title equals the queried title exactly,
# or is a near-identical long title (a one-character typo is not a different
# work; a rephrased or truncated title is never the same work).
_TITLE_SIMILARITY_THRESHOLD = 0.95
_TITLE_MIN_NORMALIZED_CHARS = 40


def _normalize_title(value: str) -> str:
    """Casefold and collapse a title to comparable word characters."""
    folded = value.casefold().replace("_", " ")
    return " ".join(re.sub(r"[^\w]+", " ", folded).split())


def _titles_match(query: str, candidate: str) -> bool:
    normalized_query = _normalize_title(query)
    normalized_candidate = _normalize_title(candidate)
    if not normalized_query or not normalized_candidate:
        return False
    if normalized_query == normalized_candidate:
        return True
    if (
        len(normalized_query) < _TITLE_MIN_NORMALIZED_CHARS
        or len(normalized_candidate) < _TITLE_MIN_NORMALIZED_CHARS
    ):
        return False
    return (
        difflib.SequenceMatcher(None, normalized_query, normalized_candidate).ratio()
        >= _TITLE_SIMILARITY_THRESHOLD
    )


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


def _split_http_url(value: str):
    """Return ``urlsplit`` parts when ``value`` is an absolute HTTP(S) URL."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    return parts


def _safe_error_url(value: str) -> str:
    """Fit a caller-supplied URL for an exception or audit context.

    Userinfo is stripped and the values of credential-like query parameters
    (``token``, ``api_key``, ``key``, ``auth``, ...) are replaced, so a
    failure never echoes the raw URL's credentials; non-URL text passes
    through unchanged.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    parts = _split_http_url(value)
    if parts is None:
        return value
    netloc = parts.netloc
    if parts.username is not None or parts.password is not None:
        netloc = netloc.rsplit("@", 1)[-1]
    query = _SENSITIVE_QUERY_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}<redacted>",
        parts.query,
    )
    try:
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except ValueError:
        return value


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


def normalize_pmid(value: str) -> str:
    """Validate and canonicalize a PMID to its bare digit string."""
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    candidate = re.sub(r"^pmid[:\s-]*", "", value.strip(), flags=re.IGNORECASE)
    if _PMID_VALUE_PATTERN.fullmatch(candidate) is None:
        raise ValueError("invalid PMID")
    return candidate


def normalize_pmcid(value: str) -> str:
    """Validate and canonicalize a PMCID to its uppercase ``PMC<digits>`` form."""
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    candidate = _PMCID_PREFIX_STRIP.sub("", value.strip())
    if _PMCID_VALUE_PATTERN.fullmatch(candidate) is None:
        raise ValueError("invalid PMCID")
    return candidate.upper()


def extract_pmid(value: str) -> str | None:
    """Extract a PMID from a genuine PubMed URL or an explicit ``PMID:`` literal.

    A bare digit string is never trusted on its own, and an identifier
    embedded in a foreign URL's query, path, or userinfo is never read: a
    URL is accepted only when its hostname is an approved NCBI PubMed host
    (matched exactly, not as a substring) and its decoded path is exactly
    the article id segment.
    """
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    candidate = value.strip()
    if not candidate:
        return None
    parts = _split_http_url(candidate)
    if parts is not None:
        if parts.username is not None or parts.password is not None:
            return None
        path = unquote(parts.path)
        host = parts.hostname or ""
        if host in _PUBMED_PAGE_HOSTS:
            match = _PMID_ARTICLE_PATH.fullmatch(path)
        elif host in _NCBI_PUBMED_HOSTS:
            match = _PUBMED_INDEX_PATH.fullmatch(path)
        else:
            return None
        return None if match is None else match.group(1)
    match = _PMID_LITERAL_PATTERN.fullmatch(candidate)
    return None if match is None else match.group(1)


def extract_pmcid(value: str) -> str | None:
    """Extract a PMCID from a genuine PMC URL or an explicit ``PMCID:`` literal.

    Mirrors :func:`extract_pmid`: only exact approved NCBI hosts with an
    anchored ``/articles/<PMC...>`` path (or ``/pmc/articles/`` on the NCBI
    indexes) are accepted; identifiers embedded in foreign URLs are never
    read.
    """
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    candidate = value.strip()
    if not candidate:
        return None
    parts = _split_http_url(candidate)
    if parts is not None:
        if parts.username is not None or parts.password is not None:
            return None
        path = unquote(parts.path)
        host = parts.hostname or ""
        if host in _PMC_PAGE_HOSTS:
            match = _PMCID_ARTICLE_PATH.fullmatch(path)
        elif host in _NCBI_PMC_HOSTS:
            match = _PMC_INDEX_PATH.fullmatch(path)
        else:
            return None
        return None if match is None else match.group(1).upper()
    match = _PMCID_LITERAL_PATTERN.fullmatch(candidate)
    return None if match is None else match.group(1).upper()


class MetadataClient(Protocol):
    async def fetch(self, doi: str, *, retrieved_at: datetime) -> "AcademicRecord": ...


@dataclass(frozen=True, slots=True)
class AcademicRecord:
    """Validated, source-neutral academic metadata.

    A record is identified by at least one public identifier: a ``doi``
    (Crossref/OpenAlex works), a ``pmid`` (PubMed), or a ``pmcid`` (PubMed
    Central).  Sources without DOIs are still honest records — Europe PMC
    covers identifier-only collections — so exactly one identifier is
    required, never a guessed one.
    """

    title: str
    source: str
    link: str
    retrieved_at: datetime
    published_at: datetime | None
    authors: tuple[str, ...]
    doi: str | None
    abstract: str | None
    access_level: str
    container_title: str | None = None
    pmid: str | None = None
    pmcid: str | None = None

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
        object.__setattr__(self, "doi", None if self.doi is None else normalize_doi(self.doi))
        object.__setattr__(self, "abstract", _optional_text(self.abstract, "abstract"))
        object.__setattr__(self, "container_title", _optional_text(self.container_title, "container_title"))
        object.__setattr__(self, "pmid", None if self.pmid is None else normalize_pmid(self.pmid))
        object.__setattr__(self, "pmcid", None if self.pmcid is None else normalize_pmcid(self.pmcid))
        if self.doi is None and self.pmid is None and self.pmcid is None:
            raise ValueError("academic records require at least one identifier (doi, pmid, or pmcid)")
        if self.access_level not in _ACCESS_LEVELS:
            raise ValueError(f"access_level must be one of {sorted(_ACCESS_LEVELS)}")
        if self.access_level == "abstract_only" and self.abstract is None:
            raise ValueError("abstract_only records require an abstract")
        if self.access_level == "metadata_only" and self.abstract is not None:
            raise ValueError("metadata_only records must not contain an abstract")

    def to_source_item(self) -> SourceItem:
        """Convert metadata to an honest source item (never full text).

        The bibliographic identity — ``authors``, ``container_title`` and the
        public identifiers (``doi``/``pmid``/``pmcid``) — is preserved so
        ingestion can keep it in the corpus frontmatter; only the body stays
        ``None``.
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
            pmid=self.pmid,
            pmcid=self.pmcid,
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


def _openalex_link(
    payload: Mapping[str, object],
    doi: str | None,
    pmcid: str | None,
    pmid: str | None,
) -> str:
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
    if doi is not None:
        return f"https://doi.org/{quote(doi, safe='')}"
    if pmcid is not None:
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    if pmid is not None:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    raise ValueError("work carries no identifier for a link")


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


def _crossref_record(
    payload: Mapping[str, object],
    url: str,
    retrieved_at: datetime,
    fallback_doi: str | None,
) -> AcademicRecord:
    try:
        title = _first_title(payload)
        raw_doi = payload.get("DOI", fallback_doi)
        if raw_doi is None:
            raise ValueError("work carries no DOI")
        result_doi = normalize_doi(raw_doi)
        published = None
        for key in ("published", "published-print", "published-online"):
            if key in payload:
                published = _parse_date_parts(payload[key], key)
                break
        abstract = _strip_jats(payload.get("abstract"))
        return AcademicRecord(
            title=title,
            source="academic",
            link=_record_link(payload, result_doi),
            retrieved_at=retrieved_at,
            published_at=published,
            authors=_crossref_authors(payload),
            doi=result_doi,
            abstract=abstract,
            access_level="abstract_only" if abstract is not None else "metadata_only",
            container_title=_first_optional(payload.get("container-title")),
        )
    except (TypeError, ValueError, KeyError) as error:
        raise SourceFetchError(FailureKind.PARSE, url, "Crossref metadata schema is invalid") from error


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
        return _crossref_record(message, url, retrieved_at, fallback_doi=normalized_doi)

    async def search(self, title: str, *, retrieved_at: datetime) -> AcademicRecord | None:
        """Find one work by strictly verifying its title; never guess a DOI.

        Crossref's bibliographic query returns ranked guesses; every hit is
        therefore checked against the queried title (normalized equality, or
        a near-identical long title) before its record is accepted.  Items
        that fail verification — or carry no identifier — are skipped.
        """
        retrieved_at = _aware(retrieved_at, "retrieved_at")
        query = _text(title, "title")
        url = f"https://api.crossref.org/works?query.bibliographic={quote(query, safe='')}&rows=5"
        root = await self._json(url)
        message = root.get("message")
        if not isinstance(message, Mapping):
            raise SourceFetchError(FailureKind.PARSE, url, "Crossref message must be an object")
        items = message.get("items")
        if not isinstance(items, list):
            raise SourceFetchError(FailureKind.PARSE, url, "Crossref message.items must be a list")
        for item in items:
            if not isinstance(item, Mapping):
                continue
            values = item.get("title")
            if not isinstance(values, list) or not values or not isinstance(values[0], str):
                continue
            if not _titles_match(query, values[0]):
                continue
            try:
                return _crossref_record(item, url, retrieved_at, fallback_doi=None)
            except SourceFetchError:
                continue
        return None


def _openalex_record(
    payload: Mapping[str, object],
    url: str,
    retrieved_at: datetime,
    fallback_doi: str | None,
) -> AcademicRecord:
    try:
        title_value = payload.get("title")
        if title_value is None:
            title_value = payload.get("display_name")
        title = _text(title_value, "title")
        ids = payload.get("ids")
        if ids is not None and not isinstance(ids, Mapping):
            raise ValueError("ids must be an object")
        result_doi = fallback_doi
        if isinstance(ids, Mapping) and ids.get("doi") is not None:
            extracted = extract_doi(ids["doi"])
            if extracted is None:
                raise ValueError("ids.doi is invalid")
            result_doi = extracted
        pmid = (
            extract_pmid(ids["pmid"])
            if isinstance(ids, Mapping) and isinstance(ids.get("pmid"), str)
            else None
        )
        pmcid = (
            extract_pmcid(ids["pmcid"])
            if isinstance(ids, Mapping) and isinstance(ids.get("pmcid"), str)
            else None
        )
        if result_doi is None and pmid is None and pmcid is None:
            raise ValueError("work carries no identifier")
        published = (
            _parse_iso_date(payload["publication_date"], "publication_date")
            if payload.get("publication_date") is not None
            else None
        )
        abstract = _reconstruct_abstract(payload.get("abstract_inverted_index"))
        return AcademicRecord(
            title=title,
            source="academic",
            link=_openalex_link(payload, result_doi, pmcid, pmid),
            retrieved_at=retrieved_at,
            published_at=published,
            authors=_openalex_authors(payload),
            doi=result_doi,
            abstract=abstract,
            access_level="abstract_only" if abstract is not None else "metadata_only",
            container_title=_openalex_venue(payload),
            pmid=pmid,
            pmcid=pmcid,
        )
    except (TypeError, ValueError, KeyError) as error:
        raise SourceFetchError(FailureKind.PARSE, url, "OpenAlex metadata schema is invalid") from error


class OpenAlexClient(_ApiClient):
    """Read public OpenAlex work metadata without credentials."""

    endpoint_host = "api.openalex.org"

    async def fetch(self, doi: str, *, retrieved_at: datetime) -> AcademicRecord:
        retrieved_at = _aware(retrieved_at, "retrieved_at")
        normalized_doi = normalize_doi(doi)
        url = f"https://api.openalex.org/works/https://doi.org/{quote(normalized_doi, safe='')}"
        root = await self._json(url)
        return _openalex_record(root, url, retrieved_at, fallback_doi=normalized_doi)

    async def search(self, title: str, *, retrieved_at: datetime) -> AcademicRecord | None:
        """Find one work by strictly verifying its title; never guess a DOI.

        Mirrors :meth:`CrossrefClient.search`: OpenAlex's ``title.search``
        results are only accepted when the hit's title is verified against
        the query, and only when the work carries a public identifier.
        """
        retrieved_at = _aware(retrieved_at, "retrieved_at")
        query = _text(title, "title")
        url = (
            "https://api.openalex.org/works?filter="
            + quote(f"title.search:{query}", safe="")
            + "&per-page=5"
        )
        root = await self._json(url)
        results = root.get("results")
        if not isinstance(results, list):
            raise SourceFetchError(FailureKind.PARSE, url, "OpenAlex results must be a list")
        for item in results:
            if not isinstance(item, Mapping):
                continue
            display = item.get("title")
            if display is None:
                display = item.get("display_name")
            if not isinstance(display, str) or not _titles_match(query, display):
                continue
            try:
                return _openalex_record(item, url, retrieved_at, fallback_doi=None)
            except SourceFetchError:
                continue
        return None


def _parse_europepmc_date(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    parts = value.split("-")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"{name} is not a valid date")
    try:
        if len(parts) == 1:
            return datetime(int(parts[0]), 1, 1, tzinfo=timezone.utc)
        if len(parts) == 2:
            return datetime(int(parts[0]), int(parts[1]), 1, tzinfo=timezone.utc)
        return datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError(f"{name} is not a valid date") from error


def _europepmc_authors(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Read Europe PMC's ``authorString`` ("Lovelace A, Hopper G.")."""
    value = payload.get("authorString")
    if value is None:
        return ()
    if not isinstance(value, str):
        raise ValueError("authorString must be a string")
    stripped = value.strip().rstrip(".")
    return tuple(part.strip() for part in stripped.split(",") if part.strip())


def _optional_identifier(value: object, normalizer, name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return normalizer(value)


def _europepmc_link(doi: str | None, pmcid: str | None, pmid: str | None) -> str:
    if doi is not None:
        return f"https://doi.org/{quote(doi, safe='')}"
    if pmcid is not None:
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    if pmid is not None:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    raise ValueError("record carries no identifier for a link")


class EuropePmcClient(_ApiClient):
    """Read public Europe PMC (PubMed/PMC) metadata without credentials.

    Europe PMC is queried by exact fielded identifier — ``EXT_ID:<pmid> AND
    SRC:MED`` or ``PMCID:<pmcid>`` — and the returned record must carry the
    requested identifier, so a lookup can never resolve to a neighbouring
    work.  Records without a DOI stay honest: they are identified by their
    PMID/PMCID and linked to the matching public landing page.
    """

    endpoint_host = "www.ebi.ac.uk"

    async def fetch(self, value: str, *, retrieved_at: datetime) -> AcademicRecord:
        retrieved_at = _aware(retrieved_at, "retrieved_at")
        url, requested_kind, requested_id = self._search_url(value)
        root = await self._json(url)
        results = root.get("resultList")
        if not isinstance(results, Mapping):
            raise SourceFetchError(FailureKind.PARSE, url, "Europe PMC resultList must be an object")
        entries = results.get("result")
        if not isinstance(entries, list) or not entries:
            raise SourceFetchError(FailureKind.PARSE, url, "no Europe PMC record matched the identifier")
        first = entries[0]
        if not isinstance(first, Mapping):
            raise SourceFetchError(FailureKind.PARSE, url, "Europe PMC result entries must be objects")
        try:
            record = self._record(first, retrieved_at)
        except (TypeError, ValueError, KeyError) as error:
            raise SourceFetchError(FailureKind.PARSE, url, "Europe PMC metadata schema is invalid") from error
        if requested_kind == "pmid" and record.pmid != requested_id:
            raise SourceFetchError(FailureKind.PARSE, url, "Europe PMC record does not match the requested PMID")
        if requested_kind == "pmcid" and record.pmcid != requested_id:
            raise SourceFetchError(FailureKind.PARSE, url, "Europe PMC record does not match the requested PMCID")
        return record

    def _search_url(self, value: str) -> tuple[str, str, str]:
        pmcid = None
        pmid = None
        try:
            pmcid = normalize_pmcid(value)
        except (TypeError, ValueError):
            try:
                pmid = normalize_pmid(value)
            except (TypeError, ValueError) as error:
                raise SourceFetchError(
                    FailureKind.PARSE,
                    _safe_error_url(value),
                    "value must be a PMID or PMCID",
                ) from error
        if pmcid is not None:
            query = f"PMCID:{pmcid}"
            kind, identifier = "pmcid", pmcid
        else:
            query = f"EXT_ID:{pmid} AND SRC:MED"
            kind, identifier = "pmid", pmid
        url = (
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query={quote(query, safe='')}&format=json&resultType=core"
        )
        return url, kind, identifier

    def _record(self, payload: Mapping[str, object], retrieved_at: datetime) -> AcademicRecord:
        pmid = _optional_identifier(payload.get("pmid"), normalize_pmid, "pmid")
        pmcid = _optional_identifier(payload.get("pmcid"), normalize_pmcid, "pmcid")
        raw_doi = payload.get("doi")
        if raw_doi is None:
            doi = None
        else:
            doi = extract_doi(raw_doi) if isinstance(raw_doi, str) else None
            if doi is None:
                raise ValueError("doi is invalid")
        first_publication = payload.get("firstPublicationDate")
        if first_publication is not None:
            published = _parse_europepmc_date(first_publication, "firstPublicationDate")
        else:
            pub_year = payload.get("pubYear")
            published = None
            if pub_year is not None:
                if isinstance(pub_year, bool) or not isinstance(pub_year, (str, int)):
                    raise ValueError("pubYear must be a string or integer")
                year_text = str(pub_year)
                if not year_text.isdigit():
                    raise ValueError("pubYear is not a valid year")
                published = datetime(int(year_text), 1, 1, tzinfo=timezone.utc)
        abstract = _optional_text(payload.get("abstractText"), "abstractText")
        return AcademicRecord(
            title=_text(payload.get("title"), "title"),
            source="academic",
            link=_europepmc_link(doi, pmcid, pmid),
            retrieved_at=retrieved_at,
            published_at=published,
            authors=_europepmc_authors(payload),
            doi=doi,
            abstract=abstract,
            access_level="abstract_only" if abstract is not None else "metadata_only",
            container_title=_optional_text(payload.get("journalTitle"), "journalTitle"),
            pmid=pmid,
            pmcid=pmcid,
        )


def _merge_openalex_abstract(record: AcademicRecord, enriched: AcademicRecord) -> AcademicRecord:
    """Upgrade a Crossref ``metadata_only`` record with an OpenAlex abstract.

    Crossref stays the bibliographic source of truth: its ``title``, ``doi``,
    ``authors``, ``container_title``, ``link`` and ``published_at`` win
    whenever they are present, so an OpenAlex enrichment never discards a
    richer Crossref bibliography.  OpenAlex only contributes the abstract
    (``enriched`` must carry one) plus the fields the Crossref record lacks —
    e.g. authors, the venue from ``primary_location.source.display_name``, a
    publication date, or the PubMed identifiers — turning a bare Crossref
    placeholder into an ``abstract_only`` record that still carries the
    Crossref identity.
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
        pmid=record.pmid if record.pmid is not None else enriched.pmid,
        pmcid=record.pmcid if record.pmcid is not None else enriched.pmcid,
    )


class ScholarlyMetadataClient:
    """Crossref-first DOI resolver with OpenAlex fallback and enrichment.

    Inputs carrying a PubMed PMID or PMC PMCID are routed to the optional
    Europe PMC client instead; a PMID/PMCID input without that client raises
    rather than pretending no identifier was present.
    """

    def __init__(
        self,
        crossref: MetadataClient,
        openalex: MetadataClient,
        europepmc: MetadataClient | None = None,
        *,
        clock,
    ) -> None:
        if not callable(getattr(crossref, "fetch", None)) or not callable(getattr(openalex, "fetch", None)):
            raise TypeError("metadata clients must provide fetch()")
        if europepmc is not None and not callable(getattr(europepmc, "fetch", None)):
            raise TypeError("metadata clients must provide fetch()")
        # A fetch-only MetadataClient (identifier resolution) is fully
        # supported: ``search`` is only demanded when :meth:`fetch_by_title`
        # actually runs.
        self._crossref = crossref
        self._openalex = openalex
        self._europepmc = europepmc
        self._clock = clock

    async def fetch(self, value: str) -> SourceItem:
        retrieved_at = _aware(self._clock(), "clock result")
        doi = extract_doi(value)
        if doi is None:
            pmcid = extract_pmcid(value)
            pmid = None if pmcid is not None else extract_pmid(value)
            if pmcid is not None or pmid is not None:
                identifier = pmcid if pmcid is not None else pmid
                return (await self._fetch_pubmed(identifier, retrieved_at)).to_source_item()
            raise SourceFetchError(
                FailureKind.PARSE,
                _safe_error_url(value),
                "no scholarly identifier found in URL",
            )
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

    async def _fetch_pubmed(self, identifier: str, retrieved_at: datetime) -> AcademicRecord:
        if self._europepmc is None:
            raise SourceFetchError(
                FailureKind.PARSE, identifier, "Europe PMC client is not configured"
            )
        return await self._europepmc.fetch(identifier, retrieved_at=retrieved_at)

    async def fetch_by_title(self, title: str, *, link: str | None = None) -> SourceItem:
        """Resolve a bibliographic record by strictly matching ``title``.

        For candidates whose URL carries no identifier, the already-known
        title is searched on Crossref (primary) and OpenAlex (fallback, at
        most once).  A record is only ever built from a search hit whose
        title verifies against the query; without a confident match this
        raises rather than guessing a DOI.  When Crossref itself fails, its
        classification survives — mirroring :meth:`fetch`.
        """
        try:
            query = _text(title, "title")
        except (TypeError, ValueError) as error:
            raise SourceFetchError(
                FailureKind.PARSE, "scholarly title search", "title must be a non-empty string"
            ) from error
        if not callable(getattr(self._crossref, "search", None)) or not callable(
            getattr(self._openalex, "search", None)
        ):
            # Title search is an opt-in capability of the bibliographic
            # clients; a fetch-only MetadataClient stays fully usable for
            # identifier resolution and only this title path refuses to run.
            raise TypeError(
                "bibliographic clients must provide search() for title-based resolution"
            )
        retrieved_at = _aware(self._clock(), "clock result")
        context = _safe_error_url(link if link is not None else query)
        crossref_error: SourceFetchError | None = None
        record: AcademicRecord | None = None
        try:
            record = await self._crossref.search(query, retrieved_at=retrieved_at)
        except SourceFetchError as error:
            crossref_error = error
        if record is None:
            try:
                record = await self._openalex.search(query, retrieved_at=retrieved_at)
            except SourceFetchError as openalex_error:
                raise (crossref_error or openalex_error) from None
        if record is None:
            if crossref_error is not None:
                raise crossref_error from None
            raise SourceFetchError(
                FailureKind.PARSE, context, "no confident bibliographic match for title"
            )
        return record.to_source_item()


__all__ = [
    "AcademicRecord",
    "CrossrefClient",
    "EuropePmcClient",
    "MetadataClient",
    "OpenAlexClient",
    "ScholarlyMetadataClient",
    "ScholarlyRecord",
    "extract_doi",
    "extract_pmcid",
    "extract_pmid",
    "normalize_doi",
    "normalize_pmcid",
    "normalize_pmid",
]
