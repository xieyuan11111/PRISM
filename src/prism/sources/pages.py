"""Public web-page extraction for the PRISM sources layer.

A minimal, dependency-free extractor built on :mod:`html.parser`: it pulls the
``<title>``, the ``meta description``, and the visible text (script/style/
template/noscript content is skipped and character references are decoded).
The result is honest about what a public page offers: ``published_at`` stays
``None`` (pages rarely announce trustworthy dates), and a page whose body or
extractable text is empty is a classified parse failure — an empty page is
never stored in place of the original (FR-1.6).
"""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .models import FailureKind, SourceFetchError, SourceItem

_SKIPPED_TAGS = frozenset({"script", "style", "template", "noscript"})
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "div", "dd", "dl",
        "dt", "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
        "pre", "section", "table", "td", "th", "tr", "ul",
    }
)
_DESCRIPTION_NAMES = frozenset({"description", "og:description", "article:description"})
_SPACE_PATTERN = re.compile(r"[ \t\f\v]+")
_ACCESS_WALL_PATTERN = re.compile(
    r"(?i)captcha|recaptcha|checking your browser|enable javascript|"
    r"access denied|please verify you are human|sign in to continue"
)
# Verification-wall detection blocks a response only when BOTH conditions
# hold:
#   1. wall phrasing appears within the first _WALL_SCAN_CHARS characters of
#      the visible text — real interstitials ("checking your browser",
#      CAPTCHA prompts, ...) announce themselves at the very top; and
#   2. the whole visible text is shorter than _MAX_WALL_TEXT_CHARS.
# The lead-only scan keeps a page that merely mentions or quotes such
# phrasing later in the article from being flagged, and the length cap keeps
# long-form pages (e.g. a survey about CAPTCHA usability, or an incident
# report quoting "Access denied") from being flagged even when they open
# with wall-like wording.  Both guards are deliberate and are part of the
# safety boundary: they make the classifier narrow (only short, top-heavy
# interstitials are blocked) rather than broad, and they must not be relaxed
# into whole-body scanning or an unbounded length check.
_WALL_SCAN_CHARS = 400
_MAX_WALL_TEXT_CHARS = 2000


class _PageParser(HTMLParser):
    """Collect title, meta description, and visible text from one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.description: str | None = None
        self._title_parts: list[str] = []
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta" and self.description is None:
            name = next(
                (value for key, value in attrs if key in ("name", "property") and value),
                None,
            )
            content = next(
                (value for key, value in attrs if key == "content" and value), None
            )
            if name is not None and name.strip().lower() in _DESCRIPTION_NAMES and content:
                self.description = content.strip()
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
            joined = "".join(self._title_parts).strip()
            if joined and self.title is None:
                self.title = _SPACE_PATTERN.sub(" ", joined)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = (_SPACE_PATTERN.sub(" ", line).strip() for line in raw.split("\n"))
        return "\n".join(line for line in lines if line)


def extract_page(body: str, *, url: str, fetched_at: datetime) -> SourceItem:
    """Extract a single :class:`SourceItem` from a public page body."""
    if not isinstance(body, str) or not body.strip():
        raise SourceFetchError(FailureKind.PARSE, url, "page body is empty")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be a timezone-aware datetime")

    parser = _PageParser()
    parser.feed(body)
    parser.close()

    host = (urlsplit(url).hostname or "").strip().lower().rstrip(".")
    text = parser.text
    if not text:
        raise SourceFetchError(
            FailureKind.PARSE, url, "page contains no extractable text"
        )
    if _ACCESS_WALL_PATTERN.search(text[:_WALL_SCAN_CHARS]) and len(text) < _MAX_WALL_TEXT_CHARS:
        raise SourceFetchError(
            FailureKind.BLOCKED, url, "page appears to be an access-verification wall"
        )
    return SourceItem(
        title=parser.title or f"Untitled page ({host or url})",
        source=host or url,
        fetched_at=fetched_at,
        link=url,
        published_at=None,
        summary=parser.description,
        content=text,
        retrieval_level="fulltext",
        access_level="fulltext",
    )


class PageFetcher:
    """:class:`SourceFetcher` implementation for public web pages."""

    kind = "page"

    def parse(
        self, body: str, *, url: str, fetched_at: datetime
    ) -> tuple[SourceItem, ...]:
        return (extract_page(body, url=url, fetched_at=fetched_at),)
