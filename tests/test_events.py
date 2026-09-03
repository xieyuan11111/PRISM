"""Focused offline contract tests for the PRISM in-process event bus."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from prism.events import DispatchError, Event, EventBus


NOW = datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def event(number: int, event_type: str = "case.created") -> Event:
    return Event(
        event_id=f"event-{number}",
        event_type=event_type,
        occurred_at=NOW,
        payload={"number": number},
        correlation_id="case-42",
    )


def test_event_is_frozen_slotted_timezone_aware_and_payload_is_safe():
    source = {
        "case_id": "case-42",
        "tags": ["policy"],
        "credentials": {"api_key": "do-not-leak", "owner": "analyst"},
    }

    created = Event("event-1", "case.created", NOW, source, "case-42")
    source["tags"].append("mutated")
    source["credentials"]["owner"] = "changed"

    assert not hasattr(created, "__dict__")
    assert created.payload["tags"] == ("policy",)
    assert created.payload["credentials"]["owner"] == "analyst"
    assert created.payload["credentials"]["api_key"] == "[REDACTED]"
    assert "do-not-leak" not in repr(created)
    with pytest.raises(FrozenInstanceError):
        created.event_type = "case.changed"
    with pytest.raises(TypeError):
        created.payload["case_id"] = "other"
    with pytest.raises(TypeError):
        created.payload["credentials"]["owner"] = "other"

    with pytest.raises(ValueError, match="timezone-aware"):
        Event("event-2", "case.created", datetime(2026, 9, 1), {}, None)


def test_subscription_ids_are_deterministic_and_unsubscribe_is_effective():
    async def scenario():
        received: list[str] = []

        async def removed_handler(item: Event) -> None:
            received.append(f"removed:{item.event_id}")

        async def active_handler(item: Event) -> None:
            received.append(item.event_id)

        bus = EventBus()
        removed_id = bus.subscribe("case.created", removed_handler)
        active_id = bus.subscribe("case.created", active_handler)

        assert removed_id == "sub-000001"
        assert active_id == "sub-000002"
        await bus.start()
        assert await bus.unsubscribe(removed_id) is True
        assert await bus.unsubscribe(removed_id) is False
        await bus.publish(event(1))
        await bus.publish(event(2, "case.updated"))
        await bus.stop()

        assert received == ["event-1"]

    run(scenario())


def test_delivery_is_ordered_per_subscriber():
    async def scenario():
        received: list[int] = []

        async def handler(item: Event) -> None:
            await asyncio.sleep(0)
            received.append(item.payload["number"])

        bus = EventBus(queue_size=2)
        bus.subscribe("case.created", handler)
        await bus.start()
        for number in range(6):
            await bus.publish(event(number))
        await bus.stop()

        assert received == list(range(6))

    run(scenario())


def test_bounded_backpressure_does_not_block_other_subscribers():
    async def scenario():
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        fast_received_all = asyncio.Event()
        slow_received: list[int] = []
        fast_received: list[int] = []

        async def slow_handler(item: Event) -> None:
            slow_received.append(item.payload["number"])
            if item.payload["number"] == 1:
                slow_started.set()
                await release_slow.wait()

        async def fast_handler(item: Event) -> None:
            fast_received.append(item.payload["number"])
            if len(fast_received) == 3:
                fast_received_all.set()

        bus = EventBus(queue_size=1)
        bus.subscribe("case.created", slow_handler)
        bus.subscribe("case.created", fast_handler)
        await bus.start()

        await bus.publish(event(1))
        await asyncio.wait_for(slow_started.wait(), timeout=1)
        await bus.publish(event(2))
        blocked_publish = asyncio.create_task(bus.publish(event(3)))

        await asyncio.wait_for(fast_received_all.wait(), timeout=1)
        assert not blocked_publish.done()
        assert fast_received == [1, 2, 3]

        release_slow.set()
        await asyncio.wait_for(blocked_publish, timeout=1)
        await bus.stop()
        assert slow_received == [1, 2, 3]

    run(scenario())


def test_handler_failure_is_isolated_and_reported():
    async def scenario():
        received: list[str] = []

        async def failing_handler(item: Event) -> None:
            raise RuntimeError(f"cannot handle {item.event_id}")

        async def healthy_handler(item: Event) -> None:
            received.append(item.event_id)

        bus = EventBus()
        failed_id = bus.subscribe("case.created", failing_handler)
        bus.subscribe("case.created", healthy_handler)
        await bus.start()
        await bus.publish(event(1))
        await bus.stop()

        assert received == ["event-1"]
        assert len(bus.errors) == 1
        error = bus.errors[0]
        assert isinstance(error, DispatchError)
        assert error.subscription_id == failed_id
        assert error.event == event(1)
        assert isinstance(error.exception, RuntimeError)
        # A subscriber failure is auditable in time, not a bare error blob.
        assert error.failed_at is not None
        assert error.failed_at.tzinfo is not None
        assert error.failed_at.utcoffset() is not None
        assert error.failed_at >= NOW

    run(scenario())


def test_dispatch_error_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        DispatchError(
            "sub-000001",
            event(1),
            RuntimeError("boom"),
            datetime(2026, 9, 1, 8, 30),
        )


def test_lifecycle_rejects_publish_while_stopped_and_leaves_no_worker_tasks():
    async def scenario():
        async def handler(item: Event) -> None:
            await asyncio.sleep(0)

        bus = EventBus()
        bus.subscribe("case.created", handler)

        with pytest.raises(RuntimeError, match="not running"):
            await bus.publish(event(1))

        await bus.start()
        workers = [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith("prism.events:")
        ]
        assert len(workers) == 1
        await bus.publish(event(1))
        await bus.stop()
        await bus.stop()

        assert all(task.done() for task in workers)
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("prism.events:")
        ]
        with pytest.raises(RuntimeError, match="not running"):
            await bus.publish(event(2))

    run(scenario())
