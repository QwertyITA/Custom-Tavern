"""Per-chat event bus.

Background passes land after the turn's request has already been answered
(§1, eventual consistency), so their results need a way back to the client that
is not tied to the request that started them. Every consumer — the turn stream
and the ambient stream — reads the same bus.

Slow or vanished subscribers are dropped rather than allowed to stall a pass.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

QUEUE_LIMIT = 256


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, chat_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers[chat_id].add(queue)
        return queue

    def unsubscribe(self, chat_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(chat_id, set()).discard(queue)
        if not self._subscribers.get(chat_id):
            self._subscribers.pop(chat_id, None)

    def publish(self, chat_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(chat_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client that cannot keep up loses events rather than
                # applying backpressure to the engine.
                self.unsubscribe(chat_id, queue)

    def subscriber_count(self, chat_id: str) -> int:
        return len(self._subscribers.get(chat_id, ()))


BUS = EventBus()
