"""Transport contract for the PRISM sources layer.

The collection layer never performs I/O itself: it depends on an injectable,
async ``HttpGetter`` so tests run entirely against fakes and production code
can plug in any transport (stdlib ``urllib`` in a thread executor, aiohttp,
...) without touching this module.  The contract deliberately exposes no way
to attach credentials, cookies, or custom headers — PRISM only collects
publicly accessible material and never bypasses access controls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One HTTP GET outcome as seen by the sources layer.

    ``url`` is the *final* URL after any redirects; the service re-validates
    it against the source whitelist so a redirect cannot smuggle the request
    to an unapproved host.
    """

    url: str
    status: int
    body: str
    content_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url must be a non-empty string")
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise TypeError("status must be an integer")
        if not isinstance(self.body, str):
            raise TypeError("body must be a string")
        if self.content_type is not None and (
            not isinstance(self.content_type, str) or not self.content_type.strip()
        ):
            raise ValueError("content_type must be a non-empty string or None")


class HttpGetter(Protocol):
    """Async HTTP GET transport injected into ``SourceService``.

    Implementations must not follow redirects to hosts the caller has not
    approved (the service re-checks ``HttpResponse.url``), must honor the
    ``timeout`` in seconds, and must never attach authentication material.
    """

    async def get(self, url: str, *, timeout: float) -> HttpResponse: ...
