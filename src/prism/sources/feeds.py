"""RSS 2.0 / Atom feed parsing for the PRISM sources layer.

Uses only :mod:`xml.etree.ElementTree` from the standard library.  Documents
carrying DTD/entity declarations are rejected outright: the stdlib parser
would expand internal entities, so feeds are limited to plain element trees.
Entry dates are parsed timezone-aware (RFC 822 for RSS ``pubDate``, ISO-8601
for Atom and Dublin Core ``date``); entries without a usable date keep
``published_at=None`` rather than an invented timestamp, and dates in the
future relative to the fetch clock are treated as untrustworthy (also
``None``) so downstream ``Material`` invariants can never be violated.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from .models import FailureKind, SourceFetchError, SourceItem

_ATOM_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"

_DECLARATION_PATTERN = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(element: ET.Element, *names: str) -> str | None:
    """Text of the first child matching ``names`` by exact or local tag name."""
    for child in element:
        if not isinstance(child.tag, str):
            continue
        if child.tag in names or _local_name(child.tag) in names:
            return (child.text or "").strip()
    return None


def _assume_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_rfc822(raw: str) -> datetime | None:
    try:
        return _assume_utc(parsedate_to_datetime(raw))
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_iso8601(raw: str) -> datetime | None:
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return _assume_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return _parse_iso8601(raw) or _parse_rfc822(raw)


def _observed_date(raw: str | None, fetched_at: datetime) -> datetime | None:
    """Keep the parsed date only when it is aware and not in the future."""
    parsed = _parse_date(raw)
    if parsed is None or parsed > fetched_at:
        return None
    return parsed


def _feed_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").strip().lower().rstrip(".")
    return host or url


def _untitled(feed_title: str | None, fallback_host: str) -> str:
    return feed_title or f"Untitled entry ({fallback_host})"


def _atom_link(entry: ET.Element, base: str) -> str | None:
    fallback: str | None = None
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if not href:
            continue
        resolved = urljoin(base, href)
        rel = (child.get("rel") or "alternate").strip()
        if rel == "alternate":
            return resolved
        if fallback is None:
            fallback = resolved
    return fallback


def _parse_rss(
    root: ET.Element, *, url: str, fetched_at: datetime
) -> tuple[SourceItem, ...]:
    host = _feed_host(url)
    channel = next(
        (child for child in root if _local_name(child.tag) == "channel"), None
    )
    if channel is None:
        raise SourceFetchError(FailureKind.PARSE, url, "rss feed has no channel element")
    feed_title = _child_text(channel, "title")

    items: list[SourceItem] = []
    for entry in channel:
        if _local_name(entry.tag) != "item":
            continue
        link_raw = _child_text(entry, "link")
        items.append(
            SourceItem(
                title=_child_text(entry, "title") or _untitled(feed_title, host),
                source=_child_text(entry, "source") or host,
                fetched_at=fetched_at,
                link=urljoin(url, link_raw) if link_raw else None,
                published_at=_observed_date(
                    _child_text(entry, "pubDate", _DC_NS + "date"), fetched_at
                ),
                summary=_child_text(entry, "description"),
                content=_child_text(entry, _ATOM_CONTENT_NS + "encoded"),
            )
        )
    return tuple(items)


def _parse_atom(
    root: ET.Element, *, url: str, fetched_at: datetime
) -> tuple[SourceItem, ...]:
    host = _feed_host(url)
    feed_title = _child_text(root, "title")

    items: list[SourceItem] = []
    for entry in root:
        if _local_name(entry.tag) != "entry":
            continue
        items.append(
            SourceItem(
                title=_child_text(entry, "title") or _untitled(feed_title, host),
                source=host,
                fetched_at=fetched_at,
                link=_atom_link(entry, url),
                published_at=_observed_date(
                    _child_text(entry, "published")
                    or _child_text(entry, "updated")
                    or _child_text(entry, _DC_NS + "date"),
                    fetched_at,
                ),
                summary=_child_text(entry, "summary"),
                content=_child_text(entry, "content"),
            )
        )
    return tuple(items)


def parse_feed(body: str, *, url: str, fetched_at: datetime) -> tuple[SourceItem, ...]:
    """Parse an RSS 2.0 or Atom document body into :class:`SourceItem` tuples.

    Raises :class:`SourceFetchError` with kind ``parse`` for empty payloads,
    DTD/entity declarations, malformed XML, or unsupported root elements.
    """
    if not isinstance(body, str) or not body.strip():
        raise SourceFetchError(FailureKind.PARSE, url, "feed body is empty")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    _require_aware(fetched_at)
    if _DECLARATION_PATTERN.search(body):
        raise SourceFetchError(
            FailureKind.PARSE, url, "feed carries DTD/entity declarations, which are rejected"
        )
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceFetchError(FailureKind.PARSE, url, f"malformed XML: {exc}") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        return _parse_rss(root, url=url, fetched_at=fetched_at)
    if root_name == "feed":
        return _parse_atom(root, url=url, fetched_at=fetched_at)
    raise SourceFetchError(
        FailureKind.PARSE, url, f"unsupported feed root element {root_name!r}; expected <rss> or <feed>"
    )


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("fetched_at must be a timezone-aware datetime")


class FeedFetcher:
    """:class:`SourceFetcher` implementation for RSS 2.0 and Atom feeds."""

    kind = "feed"

    def parse(
        self, body: str, *, url: str, fetched_at: datetime
    ) -> tuple[SourceItem, ...]:
        return parse_feed(body, url=url, fetched_at=fetched_at)
