import asyncio
from models import Event, EventType


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, asyncio.Queue] = {}

    def subscribe(self, name: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self._subscribers[name] = q
        return q

    async def publish(self, event: Event):
        for name, q in self._subscribers.items():
            if name != event.source:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    async def shutdown(self):
        evt = Event(type=EventType.SHUTDOWN, payload={}, source="bus")
        for q in self._subscribers.values():
            await q.put(evt)
