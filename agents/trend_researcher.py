"""Trend Researcher — identifies coins actively trending up and detects
inflection points where momentum is fading. Broadcasts HOT_COIN signals
so traders jump on rising coins and exit before reversals."""

from __future__ import annotations
import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType, Side
from event_bus import EventBus


class TrendResearcher:
    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("trend_researcher", topics={
            EventType.PRICE_UPDATE, EventType.SHUTDOWN,
        })

        # Price history per coin — keep 5 minutes of ticks at ~1/sec
        self.price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=300))

        # Trend state per coin
        self.trending_up: dict[str, float] = {}      # symbol -> trend_start_time
        self.trend_strength: dict[str, float] = {}    # symbol -> current strength 0-1
        self.last_broadcast: dict[str, float] = {}    # symbol -> last signal time
        self.inflection_warned: dict[str, float] = {} # symbol -> last sell signal time

    async def run(self):
        scan_task = asyncio.create_task(self._scan_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                scan_task.cancel()
                return
            if event.type == EventType.PRICE_UPDATE:
                sym = event.payload["symbol"]
                price = event.payload["price"]
                ts = event.payload["timestamp"]
                self.price_history[sym].append((ts, price))

    async def _scan_loop(self):
        """Every 10 seconds, scan all coins for trends and inflections."""
        while True:
            await asyncio.sleep(10)
            now = time.time()
            try:
                await self._scan_trends(now)
            except Exception:
                pass

    async def _scan_trends(self, now: float):
        for sym, history in self.price_history.items():
            if len(history) < 10:
                continue

            prices = [p for _, p in history]
            timestamps = [t for t, _ in history]

            # Calculate momentum over multiple windows
            momentum_10 = self._pct_change(prices, 10)   # ~10 ticks
            momentum_30 = self._pct_change(prices, 30)   # ~30 ticks
            momentum_60 = self._pct_change(prices, min(60, len(prices) - 1))

            # Velocity: recent rate of change
            velocity = momentum_10

            # Acceleration: is momentum increasing or decreasing?
            if len(prices) >= 20:
                old_momentum = self._pct_change(prices[:-10], 10)
                acceleration = momentum_10 - old_momentum
            else:
                acceleration = 0

            # === TREND UP DETECTION ===
            # Coin is trending up if: positive momentum across windows,
            # and acceleration is not strongly negative
            is_trending_up = (
                momentum_10 > 0.05 and     # Up >0.05% in last 10 ticks
                momentum_30 > 0.02 and     # Up over 30 ticks too
                acceleration > -0.15       # Not decelerating hard
            )

            if is_trending_up:
                # Calculate trend strength
                strength = min(1.0, (
                    abs(momentum_10) * 3.0 +     # Weight short-term heavily
                    abs(momentum_30) * 2.0 +
                    abs(momentum_60) * 1.0 +
                    max(acceleration, 0) * 5.0   # Bonus for accelerating
                ))
                strength = max(0.4, min(strength, 1.0))

                if sym not in self.trending_up:
                    self.trending_up[sym] = now

                self.trend_strength[sym] = strength

                # Broadcast BUY signal — every 15 seconds while trending
                last = self.last_broadcast.get(sym, 0)
                if now - last >= 15:
                    await self._emit_signal(sym, Side.BUY, strength, "trend_up", now)
                    self.last_broadcast[sym] = now
                    self.inflection_warned.pop(sym, None)

            # === INFLECTION POINT DETECTION ===
            # Coin was trending up but momentum is now fading or reversing
            elif sym in self.trending_up:
                is_inflecting = (
                    momentum_10 < -0.02 or           # Short-term reversal
                    acceleration < -0.1 or           # Strong deceleration
                    (momentum_10 < 0.02 and acceleration < 0)  # Flatlined + decelerating
                )

                if is_inflecting:
                    last_warn = self.inflection_warned.get(sym, 0)
                    if now - last_warn >= 10:
                        # Sell strength based on how bad the reversal is
                        sell_strength = min(1.0, max(0.5,
                            abs(momentum_10) * 5.0 +
                            abs(acceleration) * 3.0
                        ))
                        await self._emit_signal(sym, Side.SELL, sell_strength, "inflection", now)
                        self.inflection_warned[sym] = now

                    # Remove from trending after warning
                    del self.trending_up[sym]
                    self.trend_strength.pop(sym, None)

    def _pct_change(self, prices: list, lookback: int) -> float:
        """Percentage change over last N prices."""
        if len(prices) < lookback + 1:
            lookback = len(prices) - 1
        if lookback <= 0 or prices[-lookback - 1] == 0:
            return 0.0
        return (prices[-1] - prices[-lookback - 1]) / prices[-lookback - 1] * 100

    async def _emit_signal(self, symbol: str, direction: Side, strength: float,
                           signal_type: str, now: float):
        await self.bus.publish(Event(
            type=EventType.MOMENTUM_SIGNAL,
            payload={
                "symbol": symbol,
                "indicator": "momentum",
                "signal_type": signal_type,
                "direction": direction.value,
                "strength": round(strength, 3),
                "sensitivity": "trend_researcher",
            },
            source="trend_researcher",
        ))
