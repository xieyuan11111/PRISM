"""Pure URL helpers for the sources layer (stdlib only, no I/O).

``normalize_url`` produces the canonical form used for link-based dedup keys,
and ``host_rejection_reason`` encodes the IP/localhost half of the SSRF policy.
Both functions are total: they never raise on odd input, so callers can use
them inside security checks without fear of an error path bypassing them.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """Return a canonical form of ``url`` for stable deduplication.

    Scheme and host are lowercased, default ports and fragments are dropped,
    and a trailing slash is removed from non-root paths.  Input that cannot be
    parsed is returned stripped and unchanged rather than raising.
    """
    text = url.strip()
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
        netloc = host if port is None or _DEFAULT_PORTS.get(scheme) == port else f"{host}:{port}"
        path = parts.path or "/"
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        return urlunsplit((scheme, netloc, path, parts.query, ""))
    except ValueError:
        return text


def host_rejection_reason(host: str) -> str | None:
    """Return why ``host`` is forbidden as a fetch target, or ``None``.

    Covers localhost names and every IP-literal class that must never be
    contacted directly: loopback, private, link-local (including cloud
    metadata endpoints), unique-local, reserved, multicast, and unspecified
    addresses.  IPv4-mapped/sixtofour/teredo IPv6 forms are unwrapped first so
    they cannot smuggle a forbidden inner address past the check.
    """
    lowered = host.strip().lower().rstrip(".")
    if not lowered:
        return "an empty host"
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return "a localhost name"
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return None
    for unwrapper in ("ipv4_mapped", "sixtofour", "teredo"):
        inner = getattr(address, unwrapper, None)
        if inner is not None:
            address = inner
            break
    if address.is_private:
        return "a private IP address"
    if address.is_loopback:
        return "a loopback IP address"
    if address.is_link_local:
        return "a link-local IP address"
    if address.is_reserved:
        return "a reserved IP address"
    if address.is_multicast:
        return "a multicast IP address"
    if address.is_unspecified:
        return "an unspecified IP address"
    return None
