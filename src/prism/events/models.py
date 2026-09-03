"""Immutable contracts for PRISM in-process events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any


_REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "set_cookie",
    "token",
}
_SCALAR_TYPES = (str, int, float, bool, bytes, type(None), datetime)


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _is_secret_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_").replace(" ", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(
        ("_password", "_secret", "_token")
    )


def _freeze(value: Any, active_ids: set[int]) -> Any:
    if isinstance(value, _SCALAR_TYPES):
        return value

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("payload must not contain circular references")
        active_ids.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("payload mapping keys must be strings")
                frozen[key] = _REDACTED if _is_secret_key(key) else _freeze(item, active_ids)
            return MappingProxyType(frozen)
        finally:
            active_ids.remove(identity)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("payload must not contain circular references")
        active_ids.add(identity)
        try:
            return tuple(_freeze(item, active_ids) for item in value)
        finally:
            active_ids.remove(identity)

    if isinstance(value, (set, frozenset)):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("payload must not contain circular references")
        active_ids.add(identity)
        try:
            return frozenset(_freeze(item, active_ids) for item in value)
        finally:
            active_ids.remove(identity)

    raise TypeError(f"unsupported payload value type: {type(value).__name__}")


def normalize_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy, recursively freeze, and redact common secret-bearing fields."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return _freeze(payload, set())


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable message passed between PRISM components."""

    event_id: str
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, Any]
    correlation_id: str | None

    def __post_init__(self) -> None:
        _require_text("event_id", self.event_id)
        _require_text("event_type", self.event_type)
        if not isinstance(self.occurred_at, datetime):
            raise TypeError("occurred_at must be a datetime")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.correlation_id is not None:
            _require_text("correlation_id", self.correlation_id)
        object.__setattr__(self, "payload", normalize_payload(self.payload))


@dataclass(frozen=True, slots=True)
class DispatchError:
    """An observable record of one isolated subscriber failure.

    ``failed_at`` is the time the handler failed (defaults to now), so an
    audit trail can order and age failures instead of exposing a bare error.
    """

    subscription_id: str
    event: Event
    exception: Exception
    failed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text("subscription_id", self.subscription_id)
        if not isinstance(self.event, Event):
            raise TypeError("event must be an Event")
        if not isinstance(self.exception, Exception):
            raise TypeError("exception must be an Exception")
        failed_at = self.failed_at
        if failed_at is None:
            failed_at = datetime.now(timezone.utc)
            object.__setattr__(self, "failed_at", failed_at)
        if failed_at.tzinfo is None or failed_at.utcoffset() is None:
            raise ValueError("failed_at must be timezone-aware")
