"""A small asyncio event bus with isolated per-subscriber workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .models import DispatchError, Event


EventHandler = Callable[[Event], Awaitable[None]]


@dataclass(slots=True)
class _Subscription:
    subscription_id: str
    event_type: str
    handler: EventHandler
    queue: asyncio.Queue[Event]
    enqueue_lock: asyncio.Lock
    accepting: bool = True
    worker: asyncio.Task[None] | None = None

    async def enqueue(self, event: Event) -> None:
        async with self.enqueue_lock:
            if self.accepting:
                await self.queue.put(event)


class EventBus:
    """Dispatch events in order through a bounded queue for each subscriber."""

    def __init__(self, queue_size: int = 100) -> None:
        if isinstance(queue_size, bool) or not isinstance(queue_size, int):
            raise TypeError("queue_size must be an integer")
        if queue_size <= 0:
            raise ValueError("queue_size must be greater than zero")
        self._queue_size = queue_size
        self._next_subscription = 1
        self._subscriptions: dict[str, _Subscription] = {}
        self._errors: list[DispatchError] = []
        self._publishers: set[asyncio.Task[object]] = set()
        self._running = False

    @property
    def errors(self) -> tuple[DispatchError, ...]:
        return tuple(self._errors)

    def subscribe(self, event_type: str, handler: EventHandler) -> str:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")

        subscription_id = f"sub-{self._next_subscription:06d}"
        self._next_subscription += 1
        subscription = _Subscription(
            subscription_id=subscription_id,
            event_type=event_type,
            handler=handler,
            queue=asyncio.Queue(maxsize=self._queue_size),
            enqueue_lock=asyncio.Lock(),
        )
        self._subscriptions[subscription_id] = subscription
        if self._running:
            self._start_worker(subscription)
        return subscription_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        subscription = self._subscriptions.pop(subscription_id, None)
        if subscription is None:
            return False

        async with subscription.enqueue_lock:
            subscription.accepting = False
        await subscription.queue.join()
        await self._cancel_worker(subscription)
        return True

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for subscription in self._subscriptions.values():
            self._start_worker(subscription)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        current = asyncio.current_task()
        publishers = [task for task in self._publishers if task is not current]
        if publishers:
            await asyncio.gather(*publishers, return_exceptions=True)

        subscriptions = tuple(self._subscriptions.values())
        if subscriptions:
            await asyncio.gather(*(item.queue.join() for item in subscriptions))
            await asyncio.gather(
                *(self._cancel_worker(item) for item in subscriptions)
            )

    async def publish(self, event: Event) -> None:
        if not self._running:
            raise RuntimeError("event bus is not running")
        if not isinstance(event, Event):
            raise TypeError("event must be an Event")

        current = asyncio.current_task()
        if current is not None:
            self._publishers.add(current)
        try:
            matching = tuple(
                subscription
                for subscription in self._subscriptions.values()
                if subscription.event_type == event.event_type
            )
            if matching:
                await asyncio.gather(*(item.enqueue(event) for item in matching))
        finally:
            if current is not None:
                self._publishers.discard(current)

    def _start_worker(self, subscription: _Subscription) -> None:
        if subscription.worker is None or subscription.worker.done():
            subscription.worker = asyncio.create_task(
                self._run_subscription(subscription),
                name=f"prism.events:{subscription.subscription_id}",
            )

    async def _run_subscription(self, subscription: _Subscription) -> None:
        while True:
            event = await subscription.queue.get()
            try:
                await subscription.handler(event)
            except asyncio.CancelledError:
                raise
            except Exception as exception:
                self._errors.append(
                    DispatchError(subscription.subscription_id, event, exception)
                )
            finally:
                subscription.queue.task_done()

    @staticmethod
    async def _cancel_worker(subscription: _Subscription) -> None:
        worker = subscription.worker
        subscription.worker = None
        if worker is None:
            return
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
