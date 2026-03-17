from __future__ import annotations
import asyncio
import aiohttp
from models import Event, EventType, Side
from event_bus import EventBus


class SentimentAnalyst:
    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("sentiment", topics={EventType.SHUTDOWN})
        self.fear_greed_value: int | None = None

    async def run(self):
        fetch_task = asyncio.create_task(self._poll_loop())
        drain_task = asyncio.create_task(self._drain_queue())
        await asyncio.gather(fetch_task, drain_task)

    async def _drain_queue(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return

    async def _poll_loop(self):
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    url = "https://api.alternative.me/fng/"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                    value = int(data["data"][0]["value"])
                    self.fear_greed_value = value

                    if value < 25:
                        direction = Side.BUY
                        strength = (25 - value) / 25
                    elif value > 75:
                        direction = Side.SELL
                        strength = (value - 75) / 25
                    else:
                        await asyncio.sleep(300)
                        continue

                    for pair in self.config["trading"]["pairs"]:
                        await self.bus.publish(Event(
                            type=EventType.SENTIMENT_SIGNAL,
                            payload={
                                "symbol": pair,
                                "indicator": "sentiment",
                                "direction": direction.value,
                                "strength": min(strength, 1.0),
                                "value": value,
                            },
                            source="sentiment",
                        ))

                except (aiohttp.ClientError, KeyError, IndexError, ValueError):
                    pass
                except asyncio.CancelledError:
                    return

                await asyncio.sleep(300)
