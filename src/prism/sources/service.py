"""Whitelist-gated orchestration for PRISM public source collection.

``SourceService`` is the only entry point that performs I/O-adjacent work:
every requested URL (and every final URL reported after redirects) must pass
:class:`validate_public_url` — http(s) only, no embedded credentials, no
localhost/loopback/intranet IP targets, host present in
``PrismConfig.sources`` — before the injected async ``HttpGetter`` is called.
It then delegates parsing to pluggable :class:`~prism.sources.SourceFetcher`
strategies, deduplicates items (normalized link first, content hash
fallback), and returns ``SourceItem`` objects that callers hand to
``IngestionService``; the corpus is never written here.  Failures are
classified (:class:`FailureKind`) instead of collapsing into one error, so a
batch can record why each source failed without abandoning the rest.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from urllib.parse import urlsplit

from prism.config import PrismConfig, SourceConfig

from .feeds import FeedFetcher
from .http import HttpGetter, HttpResponse
from .models import (
    FailureKind,
    FetchFailure,
    FetchResult,
    SourceFetchError,
    SourceFetcher,
    SourceItem,
)
from .pages import PageFetcher
from .urls import host_rejection_reason

KIND_AUTO = "auto"
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ACCESS_CONTROL_STATUSES = frozenset({401, 403, 407})
_FEED_SNIFF_PATTERN = re.compile(r"<\s*(?:rss|feed)\b")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def validate_public_url(url: str, sources: SourceConfig) -> str:
    """Return the approved URL or raise a ``blocked`` :class:`SourceFetchError`.

    Enforcement order: string shape → scheme → host presence → embedded
    credentials → localhost/IP policy → whitelist.  The whitelist is checked
    last so a whitelisted-but-intranet target is still refused (the IP guards
    outrank the whitelist by design).
    """
    if not isinstance(url, str):
        raise TypeError("url must be a string")
    if _CONTROL_PATTERN.search(url):
        raise SourceFetchError(
            FailureKind.BLOCKED, url.strip() or "<empty>", "url contains control characters"
        )
    text = url.strip()
    if not text:
        raise SourceFetchError(FailureKind.BLOCKED, "<empty>", "url must not be empty")

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SourceFetchError(
            FailureKind.BLOCKED,
            text,
            f"scheme {scheme or '<missing>'!r} is not allowed; only http/https are permitted",
        )
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SourceFetchError(FailureKind.BLOCKED, text, "url has no host")
    if parts.username is not None or parts.password is not None:
        raise SourceFetchError(
            FailureKind.BLOCKED, text, "url embeds credentials, which are not permitted"
        )
    reason = host_rejection_reason(host)
    if reason is not None:
        raise SourceFetchError(
            FailureKind.BLOCKED, text, f"host {host!r} is {reason} and may not be fetched"
        )
    if not sources.allows(host):
        raise SourceFetchError(
            FailureKind.BLOCKED,
            text,
            f"host {host!r} is not in the configured source whitelist",
        )
    return text


@dataclass(frozen=True, slots=True)
class FetchBatch:
    """Per-URL outcomes of a multi-source fetch: successes plus failure records."""

    results: tuple[FetchResult, ...] = ()
    failures: tuple[FetchFailure, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "failures", tuple(self.failures))
        for result in self.results:
            if not isinstance(result, FetchResult):
                raise TypeError("results must contain only FetchResult objects")
        for failure in self.failures:
            if not isinstance(failure, FetchFailure):
                raise TypeError("failures must contain only FetchFailure objects")


def _sniff_kind(body: str) -> str:
    head = body.lstrip("\ufeff \t\r\n")[:512].lower()
    if head.startswith("<?xml") or _FEED_SNIFF_PATTERN.search(head):
        return "feed"
    return "page"


class SourceService:
    """Collect items from whitelisted public sources via an injected getter.

    The service is the SSRF gatekeeper (validation happens before any
    transport call and again on the post-redirect URL), owns deduplication
    state across fetches, and accepts additional source types as registered
    ``SourceFetcher`` strategies.  It keeps no background tasks and owns no
    resources.
    """

    def __init__(
        self,
        config: PrismConfig,
        *,
        getter: HttpGetter,
        timeout: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        fetchers: Iterable[SourceFetcher] | None = None,
    ) -> None:
        if not isinstance(config, PrismConfig):
            raise TypeError("config must be a PrismConfig")
        if not callable(getattr(getter, "get", None)):
            raise TypeError("getter must provide get()")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")

        registry: dict[str, SourceFetcher] = {}
        for fetcher in fetchers if fetchers is not None else (FeedFetcher(), PageFetcher()):
            kind = getattr(fetcher, "kind", None)
            if not isinstance(kind, str) or not kind.strip():
                raise TypeError("each fetcher must declare a non-empty kind")
            if not callable(getattr(fetcher, "parse", None)):
                raise TypeError(f"fetcher {kind!r} must provide parse()")
            if kind in registry:
                raise ValueError(f"duplicate fetcher kind: {kind!r}")
            registry[kind] = fetcher

        self._config = config
        self._getter = getter
        self._timeout = float(timeout)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        self._fetchers: MappingProxyType[str, SourceFetcher] = MappingProxyType(registry)
        self._seen: set[str] = set()

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def seen_keys(self) -> frozenset[str]:
        return frozenset(self._seen)

    @property
    def fetcher_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._fetchers))

    def reset_dedup(self) -> None:
        """Forget collected dedup keys so previously seen items refetch."""
        self._seen.clear()

    def validate_url(self, url: str) -> str:
        """Validate ``url`` against this service's configured whitelist."""
        return validate_public_url(url, self._config.sources)

    async def fetch(self, url: str, *, kind: str = KIND_AUTO) -> FetchResult:
        """Fetch one whitelisted URL and return its new, deduplicated items."""
        if not isinstance(kind, str) or not kind.strip():
            raise TypeError("kind must be a non-empty string")
        if kind != KIND_AUTO and kind not in self._fetchers:
            available = ", ".join((KIND_AUTO, *self._fetchers))
            raise ValueError(f"unknown kind {kind!r}; available kinds: {available}")

        validated = self.validate_url(url)
        fetched_at = self._now()
        response = await self._request(validated)

        chosen = kind if kind != KIND_AUTO else _sniff_kind(response.body)
        fetcher = self._fetchers.get(chosen)
        if fetcher is None:
            raise SourceFetchError(
                FailureKind.PARSE,
                validated,
                f"no fetcher registered for detected payload kind {chosen!r}",
            )
        parsed = fetcher.parse(response.body, url=validated, fetched_at=fetched_at)
        if not isinstance(parsed, tuple):
            raise TypeError(f"fetcher {chosen!r} parse() must return a tuple")

        new_items: list[SourceItem] = []
        duplicate_keys: list[str] = []
        for item in parsed:
            if not isinstance(item, SourceItem):
                raise TypeError(
                    f"fetcher {chosen!r} parse() must return only SourceItem objects"
                )
            key = item.dedup_key
            if key in self._seen:
                duplicate_keys.append(key)
            else:
                self._seen.add(key)
                new_items.append(item)
        return FetchResult(
            url=validated,
            fetched_at=fetched_at,
            items=tuple(new_items),
            duplicate_keys=tuple(duplicate_keys),
        )

    async def fetch_all(
        self, urls: Iterable[str], *, kind: str = KIND_AUTO
    ) -> FetchBatch:
        """Fetch many sources, recording classified per-URL failures."""
        results: list[FetchResult] = []
        failures: list[FetchFailure] = []
        for url in urls:
            try:
                results.append(await self.fetch(url, kind=kind))
            except SourceFetchError as exc:
                failures.append(exc.as_failure())
        return FetchBatch(tuple(results), tuple(failures))

    async def _request(self, url: str) -> HttpResponse:
        """Call the getter, mapping every failure mode to its FailureKind."""
        try:
            response = await self._getter.get(url, timeout=self._timeout)
        except TimeoutError as exc:
            raise SourceFetchError(
                FailureKind.TIMEOUT, url, f"request timed out after {self._timeout:g}s"
            ) from exc
        except SourceFetchError:
            raise
        except Exception as exc:
            raise SourceFetchError(
                FailureKind.TRANSPORT, url, f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(response, HttpResponse):
            raise SourceFetchError(
                FailureKind.TRANSPORT,
                url,
                f"getter must return an HttpResponse, got {type(response).__name__}",
            )
        try:
            self.validate_url(response.url)
        except SourceFetchError as exc:
            raise SourceFetchError(
                FailureKind.BLOCKED, url, f"final URL rejected: {exc.detail}"
            ) from exc
        if not 200 <= response.status < 300:
            if response.status in _ACCESS_CONTROL_STATUSES:
                detail = (
                    f"HTTP {response.status}: access requires authentication or"
                    " permission; PRISM never bypasses access controls"
                    " (login walls, paywalls)"
                )
            else:
                detail = f"HTTP {response.status}"
            raise SourceFetchError(FailureKind.HTTP_STATUS, url, detail)
        return response

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise RuntimeError("clock must return timezone-aware datetimes")
        return value
