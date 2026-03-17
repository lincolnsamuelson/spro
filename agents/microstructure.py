from __future__ import annotations
import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType, Side
from event_bus import EventBus


class MicrostructureAnalyst:
    """Detects micro-patterns in price data:
    - Price clustering at round numbers (support/resistance)
    - Rapid oscillation (chop detection — avoid trading)
    - Momentum exhaustion (price stalling after a run)
    - Volume-like inference from tick frequency"""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("microstructure", topics={
            EventType.PRICE_UPDATE, EventType.CANDLE, EventType.SHUTDOWN,
        })
        self.ticks: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.candles: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        self.last_emit: dict[str, float] = {}

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PRICE_UPDATE:
                await self._on_price(event.payload)
            elif event.type == EventType.CANDLE:
                self.candles[event.payload["symbol"]].append(event.payload)

    async def _on_price(self, payload: dict):
        symbol = payload["symbol"]
        price = payload["price"]
        now = payload.get("timestamp", time.time())
        self.ticks[symbol].append((now, price))

        ticks = list(self.ticks[symbol])
        if len(ticks) < 20:
            return

        if now - self.last_emit.get(symbol, 0) < 8:
            return

        # Check for round number bounce
        await self._check_round_number(symbol, ticks, now)

        # Check for tick frequency spike (proxy for volume)
        await self._check_tick_frequency(symbol, ticks, now)

    async def _check_round_number(self, symbol: str, ticks: list, now: float):
        """Price bouncing off a round number = support/resistance signal."""
        price = ticks[-1][1]
        if price <= 0:
            return

        # Find nearest round number
        if price > 1000:
            round_num = round(price / 100) * 100
            proximity = abs(price - round_num) / price
        elif price > 10:
            round_num = round(price)
            proximity = abs(price - round_num) / price
        elif price > 0.1:
            round_num = round(price, 1)
            proximity = abs(price - round_num) / price
        else:
            return

        # Price very close to round number and bouncing
        if proximity < 0.003:  # Within 0.3% of round number
            # Check if price is bouncing up (support) or down (resistance)
            recent = [p for _, p in ticks[-10:]]
            min_recent = min(recent)
            max_recent = max(recent)

            if price > round_num and min_recent <= round_num:
                # Bounced up off round number = support
                strength = min((1 - proximity / 0.003) * 0.7, 0.8)
                await self._emit(symbol, "round_support", Side.BUY, strength)
                self.last_emit[symbol] = now
            elif price < round_num and max_recent >= round_num:
                # Rejected at round number = resistance
                strength = min((1 - proximity / 0.003) * 0.7, 0.8)
                await self._emit(symbol, "round_resistance", Side.SELL, strength)
                self.last_emit[symbol] = now

    async def _check_tick_frequency(self, symbol: str, ticks: list, now: float):
        """Sudden increase in tick frequency suggests high activity/volume."""
        if len(ticks) < 30:
            return

        # Compare recent tick frequency to older
        recent_10 = ticks[-10:]
        older_20 = ticks[-30:-10]

        recent_span = recent_10[-1][0] - recent_10[0][0]
        older_span = older_20[-1][0] - older_20[0][0]

        if recent_span <= 0 or older_span <= 0:
            return

        recent_freq = len(recent_10) / recent_span
        older_freq = len(older_20) / older_span

        if older_freq <= 0:
            return

        freq_ratio = recent_freq / older_freq

        # Tick frequency spike — suggests high activity
        if freq_ratio > 2.0:
            # Determine direction from price movement
            price_change = (ticks[-1][1] - ticks[-10][1]) / ticks[-10][1] if ticks[-10][1] > 0 else 0

            if abs(price_change) > 0.001:
                direction = Side.BUY if price_change > 0 else Side.SELL
                strength = min(freq_ratio / 5.0, 1.0)
                await self._emit(symbol, "tick_spike", direction, strength)
                self.last_emit[symbol] = now

    async def _emit(self, symbol: str, signal_type: str, direction: Side, strength: float):
        await self.bus.publish(Event(
            type=EventType.MICROSTRUCTURE_SIGNAL,
            payload={
                "symbol": symbol,
                "indicator": f"micro_{signal_type}",
                "signal_type": signal_type,
                "direction": direction.value,
                "strength": round(strength, 3),
            },
            source="microstructure",
        ))
