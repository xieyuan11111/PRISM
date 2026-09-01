"""Firecrawl-backed adapter for the PRISM research SearchProvider seam.

``FirecrawlSearchProvider`` executes one planned
:class:`~prism.research.models.SearchQuery` against the official Firecrawl v2
API — ``POST /v2/search`` for ranked search results and ``POST /v2/map`` for
site-URL candidates — through an injected async JSON client.  This module is
dependency-free and never performs I/O itself: the transport is a pluggable
:class:`JsonClient`, so tests run entirely against fakes.

Security posture (mirrors the sources layer, REQUIREMENTS NFR-6 / FR-1.15):

* the API key is injected via the constructor or ``FIRECRAWL_API_KEY`` and
  travels only in the ``Authorization`` header — never in a request body,
  ``repr``, or error detail (transport wrapping redacts it defensively);
* the request body carries explicit fields only (``query``/``limit``/``tbs``/
  ``scrapeOptions``, or ``url``/``limit``/``includeSubdomains``) and never
  local paths or private material;
* every returned URL is re-validated with
  :func:`prism.sources.validate_public_url` (whitelist + SSRF policy) and the
  host must additionally fall inside the query's ``source_domains`` scope;
  if the client followed a redirect that left the configured Firecrawl host
  the response is rejected wholesale.

Results are deduplicated by normalized link, truncated to the rank-ordered
limit, then sorted by normalized link so output is deterministic.  Firecrawl
output is *discovery*, not evidence: search results keep scraped markdown as
``content`` for later re-collection, map candidates are URL-only
(``content``/``summary`` stay ``None``, ``type`` is
:data:`MAP_CANDIDATE_TYPE`), and everything is handed onward to
:class:`~prism.ingestion.IngestionService` for the authoritative fetch.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from prism.config import PrismConfig
from prism.sources import (
    FailureKind,
    SourceFetchError,
    SourceItem,
    normalize_url,
    validate_public_url,
)

from .models import SearchQuery

DEFAULT_BASE_URL = "https://api.firecrawl.dev"
SEARCH_PATH = "/v2/search"
MAP_PATH = "/v2/map"
FIRECRAWL_API_KEY_ENV = "FIRECRAWL_API_KEY"
MAP_CANDIDATE_TYPE = "candidate_url"
DEFAULT_LIMIT = 10
MIN_LIMIT = 1
MAX_LIMIT = 100
_REDACTED = "[redacted]"
_ALLOWED_BASE_SCHEMES = frozenset({"http", "https"})


class FirecrawlError(Exception):
    """Base class for Firecrawl adapter failures, mapped onto FailureKind."""

    kind: FailureKind = FailureKind.TRANSPORT

    def __init__(self, detail: str) -> None:
        super().__init__(f"firecrawl {self.kind.value} error: {detail}")
        self.detail = detail


class FirecrawlHttpError(FirecrawlError):
    """The API answered with a non-2xx HTTP status."""

    kind = FailureKind.HTTP_STATUS

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class FirecrawlJsonError(FirecrawlError):
    """The response body was not valid JSON."""

    kind = FailureKind.PARSE


class FirecrawlSchemaError(FirecrawlError):
    """The body parsed as JSON but did not match the API response shape."""

    kind = FailureKind.PARSE


class FirecrawlBlockedError(FirecrawlError):
    """A URL (endpoint, target, or link) violated the SSRF/whitelist policy."""

    kind = FailureKind.BLOCKED


class FirecrawlTransportError(FirecrawlError):
    """The injected client failed below the HTTP layer."""

    kind = FailureKind.TRANSPORT


class FirecrawlTimeoutError(FirecrawlError):
    """The request exceeded its timeout budget."""

    kind = FailureKind.TIMEOUT


@dataclass(frozen=True, slots=True)
class JsonHttpResponse:
    """One HTTP POST outcome as seen by the Firecrawl adapter.

    ``url`` is the *final* URL after any redirects; the provider checks it
    stayed on the configured Firecrawl host so a redirect cannot smuggle the
    API call (and the Authorization header) to a foreign server.  ``body`` is
    the raw payload text; JSON parsing stays with the provider so JSON errors
    stay distinguishable from schema errors.
    """

    url: str
    status: int
    body: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url must be a non-empty string")
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise TypeError("status must be an integer")
        if not isinstance(self.body, str):
            raise TypeError("body must be a string")


class JsonClient(Protocol):
    """Async JSON POST transport injected into ``FirecrawlSearchProvider``.

    Implementations must honor ``timeout`` in seconds, serialize ``json_body``
    as UTF-8 JSON, and report the final URL after redirects.  They must not
    attach anything beyond the passed headers — the provider owns the
    Authorization header and keeps the key out of every other channel.
    """

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any],
        timeout: float,
    ) -> JsonHttpResponse: ...


def _resolve_api_key(api_key: str | None) -> str:
    if api_key is not None and not isinstance(api_key, str):
        raise TypeError("api_key must be a string or None")
    if api_key is None:
        api_key = os.environ.get(FIRECRAWL_API_KEY_ENV, "")
    key = api_key.strip()
    if not key:
        raise ValueError(
            "FirecrawlSearchProvider needs an API key: pass api_key=... or "
            f"set the {FIRECRAWL_API_KEY_ENV} environment variable"
        )
    return key


def _normalize_base_url(base_url: str) -> str:
    if isinstance(base_url, bool) or not isinstance(base_url, str):
        raise TypeError("base_url must be a string")
    text = base_url.strip()
    if not text:
        raise ValueError("base_url must not be empty")
    text = text.rstrip("/")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError as error:
        raise ValueError("base_url must be an http(s) URL with a host") from error
    if parts.scheme.lower() not in _ALLOWED_BASE_SCHEMES or not host:
        raise ValueError("base_url must be an http(s) URL with a host")
    return text


def _check_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}")
    return limit


def _check_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a number")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    return float(timeout)


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""


def _window_tbs(window: Any) -> str:
    """Encode a research window as Firecrawl's Google-style date-range ``tbs``."""
    start = window.start_at.astimezone(timezone.utc)
    end = window.end_at.astimezone(timezone.utc)
    return f"cdr:1,cd_min:{start:%m/%d/%Y},cd_max:{end:%m/%d/%Y}"


def _parse_published(value: str, fetched_at: datetime) -> datetime | None:
    """Parse an optional ISO-8601 publish time; never invent a date.

    Naive timestamps and dates in the future of the fetch are discarded
    (``None``) rather than guessed or clamped, matching the ``SourceItem``
    contract that unknown publish times stay unknown.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    if parsed > fetched_at:
        return None
    return parsed


def _settle(items: list[SourceItem], limit: int) -> tuple[SourceItem, ...]:
    """Dedup by normalized link (first payload wins), cap, then sort by link."""
    seen: set[str] = set()
    unique: list[SourceItem] = []
    for item in items:
        key = normalize_url(item.link or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique = unique[:limit]
    unique.sort(key=lambda item: normalize_url(item.link or ""))
    return tuple(unique)


def _search_entries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read Firecrawl v2 search result arrays, with legacy data compatibility."""
    arrays: list[Any] = []
    for key in ("web", "news", "images"):
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, list):
                raise FirecrawlSchemaError(f"search response field {key!r} must be a list")
            arrays.extend(value)
    if not arrays and "data" in payload:
        data = payload.get("data")
        if not isinstance(data, list):
            raise FirecrawlSchemaError("search response field 'data' must be a list")
        arrays = data
    if not arrays and not any(key in payload for key in ("web", "news", "images", "data")):
        raise FirecrawlSchemaError("search response has no result array")
    for index, raw in enumerate(arrays):
        if not isinstance(raw, dict):
            raise FirecrawlSchemaError(f"search result #{index} must be a JSON object")
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise FirecrawlSchemaError(
                f"search result #{index} needs a non-empty string 'url'"
            )
        for field in ("title", "description", "markdown"):
            if raw.get(field) is not None and not isinstance(raw[field], str):
                raise FirecrawlSchemaError(
                    f"search result #{index} field {field!r} must be a string"
                )
        if raw.get("publishedDate") is not None and not isinstance(
            raw["publishedDate"], str
        ):
            raise FirecrawlSchemaError(
                f"search result #{index} field 'publishedDate' must be a string"
            )
    return list(arrays)


def _map_links(payload: Mapping[str, Any]) -> list[str]:
    links = payload.get("links")
    if not isinstance(links, list):
        raise FirecrawlSchemaError("map response field 'links' must be a list")
    for index, link in enumerate(links):
        if not isinstance(link, str) or not link.strip():
            raise FirecrawlSchemaError(f"map link #{index} must be a non-empty string")
    return list(links)


class FirecrawlSearchProvider:
    """Execute planned search queries against the Firecrawl v2 API.

    Satisfies the :class:`~prism.research.provider.SearchProvider` protocol.
    ``search`` posts one query (explicit ``query``/``limit``/``tbs``/
    ``scrapeOptions`` body) and maps ranked results to
    :class:`~prism.sources.SourceItem` objects whose links re-passed the full
    whitelist/SSRF policy *and* the query's own ``source_domains`` scope;
    ``map_site`` returns URL-only candidates for one whitelisted site.  Items
    are discovery leads: the authoritative fetch still belongs to
    :class:`~prism.ingestion.IngestionService`.
    """

    name = "firecrawl"

    def __init__(
        self,
        config: PrismConfig,
        *,
        client: JsonClient,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        limit: int = DEFAULT_LIMIT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, PrismConfig):
            raise TypeError("config must be a PrismConfig")
        if not callable(getattr(client, "post", None)):
            raise TypeError("client must provide post()")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._config = config
        self._client = client
        self._api_key = _resolve_api_key(api_key)
        self._base_url = _normalize_base_url(base_url)
        self._limit = _check_limit(limit)
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )

    def __repr__(self) -> str:
        return (
            f"FirecrawlSearchProvider(base_url={self._base_url!r}, "
            f"limit={self._limit}, api_key=<{_REDACTED}>)"
        )

    @property
    def search_endpoint(self) -> str:
        return self._base_url + SEARCH_PATH

    @property
    def map_endpoint(self) -> str:
        return self._base_url + MAP_PATH

    async def search(
        self, query: SearchQuery, *, timeout: float = 10.0
    ) -> tuple[SourceItem, ...]:
        """Return whitelist-validated source items for one planned query."""
        if not isinstance(query, SearchQuery):
            raise TypeError("query must be a SearchQuery")
        seconds = _check_timeout(timeout)

        allowed = frozenset(
            domain
            for domain in query.source_domains
            if self._config.sources.allows(domain)
        )
        if not allowed:
            # Nothing the engine can return would survive the policy gate, so
            # skip the request entirely instead of spending credits.
            return ()

        body = {
            "query": query.query,
            "limit": self._limit,
            "tbs": _window_tbs(query.window),
            "includeDomains": sorted(allowed),
            "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
        }
        payload = await self._request(self.search_endpoint, body, seconds)
        entries = _search_entries(payload)
        fetched_at = self._now()

        items: list[SourceItem] = []
        for raw in entries:
            try:
                link = validate_public_url(raw["url"], self._config.sources)
            except SourceFetchError:
                continue
            host = _host_of(link)
            if host not in allowed:
                continue
            published = raw.get("publishedDate")
            items.append(
                SourceItem(
                    title=raw.get("title") or link,
                    source=host,
                    fetched_at=fetched_at,
                    link=link,
                    published_at=(
                        _parse_published(published, fetched_at) if published else None
                    ),
                    summary=raw.get("description"),
                    content=raw.get("markdown"),
                    type=query.source_types[0],
                )
            )
        return _settle(items, self._limit)

    async def map_site(
        self, url: str, *, limit: int | None = None, timeout: float = 10.0
    ) -> tuple[SourceItem, ...]:
        """Return URL-only candidate items discovered for one whitelisted site.

        Candidates carry no content or summary — they are leads for a later
        authoritative fetch, never stand-ins for fetched material — and are
        marked with :data:`MAP_CANDIDATE_TYPE`.  Links off the mapped host or
        outside the whitelist/SSRF policy are dropped.
        """
        if not isinstance(url, str):
            raise TypeError("url must be a string")
        seconds = _check_timeout(timeout)
        effective_limit = self._limit if limit is None else _check_limit(limit)
        try:
            target = validate_public_url(url, self._config.sources)
        except SourceFetchError as exc:
            raise FirecrawlBlockedError(f"map target rejected: {exc.detail}") from exc

        body = {"url": target, "limit": effective_limit, "includeSubdomains": False}
        payload = await self._request(self.map_endpoint, body, seconds)
        links = _map_links(payload)
        fetched_at = self._now()
        target_host = _host_of(target)

        items: list[SourceItem] = []
        for link in links:
            try:
                validated = validate_public_url(link, self._config.sources)
            except SourceFetchError:
                continue
            host = _host_of(validated)
            if host != target_host:
                continue
            items.append(
                SourceItem(
                    title=validated,
                    source=host,
                    fetched_at=fetched_at,
                    link=validated,
                    type=MAP_CANDIDATE_TYPE,
                )
            )
        return _settle(items, effective_limit)

    async def _request(
        self, endpoint: str, body: Mapping[str, Any], timeout: float
    ) -> dict[str, Any]:
        """POST one JSON request and classify every failure mode."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.post(
                endpoint, headers=headers, json_body=dict(body), timeout=timeout
            )
        except TimeoutError as exc:
            raise FirecrawlTimeoutError(
                f"request to {endpoint} timed out after {timeout:g}s"
            ) from None
        except Exception as exc:
            raise FirecrawlTransportError(
                self._redact(f"{type(exc).__name__}: {exc}")
            ) from None
        if not isinstance(response, JsonHttpResponse):
            raise FirecrawlTransportError(
                f"client must return a JsonHttpResponse for {endpoint}, "
                f"got {type(response).__name__}"
            )
        if _host_of(response.url) != _host_of(endpoint):
            raise FirecrawlBlockedError(
                f"final URL {response.url!r} left the configured Firecrawl host"
            )
        if not 200 <= response.status < 300:
            raise FirecrawlHttpError(
                response.status, self._redact(f"request to {endpoint} failed")
            )
        try:
            payload = json.loads(response.body)
        except ValueError as exc:
            raise FirecrawlJsonError(
                self._redact(f"response from {endpoint} is not valid JSON: {exc}")
            ) from exc
        if not isinstance(payload, dict):
            raise FirecrawlSchemaError(
                f"response from {endpoint} must be a JSON object"
            )
        if payload.get("success") is False:
            raise FirecrawlSchemaError(
                f"response from {endpoint} reports success=false"
            )
        return payload

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, _REDACTED)

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise RuntimeError("clock must return timezone-aware datetimes")
        return value


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_LIMIT",
    "FIRECRAWL_API_KEY_ENV",
    "FirecrawlBlockedError",
    "FirecrawlError",
    "FirecrawlHttpError",
    "FirecrawlJsonError",
    "FirecrawlSchemaError",
    "FirecrawlSearchProvider",
    "FirecrawlTimeoutError",
    "FirecrawlTransportError",
    "JsonClient",
    "JsonHttpResponse",
    "MAP_CANDIDATE_TYPE",
    "MAX_LIMIT",
    "MIN_LIMIT",
]
