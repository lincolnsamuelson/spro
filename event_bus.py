from __future__ import annotations
import asyncio
from models import Event, EventType


class EventBus:
    """Topic-filtered pub/sub. Subscribers can filter by event type to avoid
    queue flooding. Drops oldest event on full queue instead of dropping new."""

    def __init__(self):
        self._subscribers: dict[str, tuple[asyncio.Queue, set[EventType] | None]] = {}

    def subscribe(self, name: str, topics: set[EventType] | None = None,
                  maxsize: int = 2000) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=maxsize)
        self._subscribers[name] = (q, topics)
        return q

    async def publish(self, event: Event):
        for name, (q, topics) in self._subscribers.items():
            if name != event.source:
                if topics is not None and event.type not in topics:
                    continue
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest to make room for fresh data
                    try:
                        q.get_nowait()
                        q.put_nowait(event)
                    except asyncio.QueueEmpty:
                        pass

    async def shutdown(self):
        evt = Event(type=EventType.SHUTDOWN, payload={}, source="bus")
        for _, (q, _) in self._subscribers.items():
            await q.put(evt)
