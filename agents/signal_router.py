from __future__ import annotations
import asyncio
from models import Event, EventType, RESEARCHER_SIGNAL_TYPES
from event_bus import EventBus


class SignalRouter:
    """Receives all researcher signals, filters blacklisted coins, and fans out
    to all trader agents. No bottleneck — each trader decides independently."""

    def __init__(self, bus: EventBus, config: dict, traders: list):
        self.bus = bus
        self.config = config
        self.traders = traders  # list of TraderAgent instances
        self.blacklist: set = set()
        self.probation: set = set()
        self.leverage_overrides: dict[str, int] = {}
        self.bad_combos: dict[str, float] = {}

        topics = RESEARCHER_SIGNAL_TYPES | {
            EventType.STRATEGY_ADJUSTMENT,
            EventType.COMPOUND_TRIGGER,
            EventType.SHUTDOWN,
        }
        self.queue = bus.subscribe("signal_router", topics=topics)

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                # Forward shutdown to all traders
                for trader in self.traders:
                    try:
                        trader.signal_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
                return

            if event.type == EventType.STRATEGY_ADJUSTMENT:
                self._apply_adjustment(event.payload)
                # Forward to traders so they update their intelligence
                for trader in self.traders:
                    try:
                        trader.signal_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
                continue

            if event.type == EventType.COMPOUND_TRIGGER:
                # Forward to all traders — each checks its own idle capital
                for trader in self.traders:
                    try:
                        trader.signal_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
                continue

            # Filter blacklisted coins
            symbol = event.payload.get("symbol", "")
            if symbol in self.blacklist:
                continue

            # Fan out to ALL traders — each decides based on its own style
            for trader in self.traders:
                try:
                    trader.signal_queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest to make room
                    try:
                        trader.signal_queue.get_nowait()
                        trader.signal_queue.put_nowait(event)
                    except asyncio.QueueEmpty:
                        pass

    def _apply_adjustment(self, payload: dict):
        self.blacklist = set(payload.get("blacklist", []))
        self.probation = set(payload.get("probation", []))
        self.leverage_overrides = payload.get("leverage_overrides", {})
        self.bad_combos = payload.get("bad_combos", {})
