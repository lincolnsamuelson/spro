from __future__ import annotations
import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType, Side
from event_bus import EventBus


class OrderFlowAnalyst:
    """Analyzes tick-level buying/selling pressure from price movements.
    Tracks consecutive up-ticks vs down-ticks to detect aggressive buyers/sellers."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("order_flow", topics={
            EventType.PRICE_UPDATE, EventType.SHUTDOWN,
        })
        # Track last N price ticks per symbol
        self.ticks: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        self.last_emit: dict[str, float] = {}

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PRICE_UPDATE:
                await self._on_price(event.payload)

    async def _on_price(self, payload: dict):
        symbol = payload["symbol"]
        price = payload["price"]
        now = payload.get("timestamp", time.time())
        self.ticks[symbol].append((now, price))

        ticks = list(self.ticks[symbol])
        if len(ticks) < 10:
            return

        # Rate limit: max 1 signal per 5 seconds per symbol
        if now - self.last_emit.get(symbol, 0) < 5:
            return

        # Count consecutive up/down ticks in last 15 ticks
        recent = ticks[-15:]
        up_count = 0
        down_count = 0
        for i in range(1, len(recent)):
            if recent[i][1] > recent[i-1][1]:
                up_count += 1
            elif recent[i][1] < recent[i-1][1]:
                down_count += 1

        total = up_count + down_count
        if total < 5:
            return

        buy_pressure = up_count / total
        sell_pressure = down_count / total

        # Strong directional pressure
        if buy_pressure >= 0.70:
            strength = min((buy_pressure - 0.5) * 2, 1.0)
            await self._emit(symbol, Side.BUY, strength)
            self.last_emit[symbol] = now
        elif sell_pressure >= 0.70:
            strength = min((sell_pressure - 0.5) * 2, 1.0)
            await self._emit(symbol, Side.SELL, strength)
            self.last_emit[symbol] = now

    async def _emit(self, symbol: str, direction: Side, strength: float):
        await self.bus.publish(Event(
            type=EventType.ORDER_FLOW_SIGNAL,
            payload={
                "symbol": symbol,
                "indicator": "order_flow",
                "direction": direction.value,
                "strength": round(strength, 3),
            },
            source="order_flow",
        ))
