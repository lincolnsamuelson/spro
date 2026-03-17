from __future__ import annotations
import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType, Side
from event_bus import EventBus


class CorrelationAnalyst:
    """Tracks rolling correlation between BTC and altcoins.
    When an altcoin diverges from BTC (correlation break), it signals
    either a mean-reversion opportunity or a breakout."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("correlation", topics={
            EventType.PRICE_UPDATE, EventType.SHUTDOWN,
        })
        self.returns: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self.last_prices: dict[str, float] = {}
        self.last_emit: dict[str, float] = {}

    async def run(self):
        scan_task = asyncio.create_task(self._scan_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                scan_task.cancel()
                return
            if event.type == EventType.PRICE_UPDATE:
                self._on_price(event.payload)

    def _on_price(self, payload: dict):
        symbol = payload["symbol"]
        price = payload["price"]
        if symbol in self.last_prices and self.last_prices[symbol] > 0:
            ret = (price - self.last_prices[symbol]) / self.last_prices[symbol]
            self.returns[symbol].append(ret)
        self.last_prices[symbol] = price

    async def _scan_loop(self):
        """Every 20 seconds, check for correlation breaks."""
        while True:
            await asyncio.sleep(20)
            await self._analyze()

    async def _analyze(self):
        btc_returns = list(self.returns.get("bitcoin", []))
        if len(btc_returns) < 10:
            return

        now = time.time()
        for symbol, returns_dq in self.returns.items():
            if symbol == "bitcoin":
                continue
            if now - self.last_emit.get(symbol, 0) < 15:
                continue

            alt_returns = list(returns_dq)
            if len(alt_returns) < 10:
                continue

            # Use the shorter length
            n = min(len(btc_returns), len(alt_returns))
            btc = btc_returns[-n:]
            alt = alt_returns[-n:]

            # Compute correlation
            mean_btc = sum(btc) / n
            mean_alt = sum(alt) / n
            cov = sum((b - mean_btc) * (a - mean_alt) for b, a in zip(btc, alt)) / n
            var_btc = sum((b - mean_btc) ** 2 for b in btc) / n
            var_alt = sum((a - mean_alt) ** 2 for a in alt) / n

            if var_btc == 0 or var_alt == 0:
                continue

            correlation = cov / ((var_btc ** 0.5) * (var_alt ** 0.5))

            # Detect divergence: altcoin moving opposite to BTC
            recent_btc = sum(btc[-5:])
            recent_alt = sum(alt[-5:])

            # Strong divergence: correlation normally positive but recent moves are opposite
            if correlation > 0.3 and abs(recent_alt - recent_btc) > 0.005:
                if recent_alt > recent_btc + 0.003:
                    # Alt outperforming BTC — momentum buy
                    strength = min(abs(recent_alt - recent_btc) * 50, 1.0)
                    await self._emit(symbol, Side.BUY, strength)
                    self.last_emit[symbol] = now
                elif recent_alt < recent_btc - 0.003:
                    # Alt underperforming — possible sell
                    strength = min(abs(recent_btc - recent_alt) * 50, 1.0)
                    await self._emit(symbol, Side.SELL, strength)
                    self.last_emit[symbol] = now

            # Decorrelation event: normally correlated coin suddenly diverging
            elif correlation < -0.2 and abs(recent_alt) > 0.003:
                direction = Side.BUY if recent_alt > 0 else Side.SELL
                strength = min(abs(correlation) * abs(recent_alt) * 100, 1.0)
                if strength > 0.3:
                    await self._emit(symbol, direction, strength)
                    self.last_emit[symbol] = now

    async def _emit(self, symbol: str, direction: Side, strength: float):
        await self.bus.publish(Event(
            type=EventType.CORRELATION_SIGNAL,
            payload={
                "symbol": symbol,
                "indicator": "correlation",
                "direction": direction.value,
                "strength": round(strength, 3),
            },
            source="correlation",
        ))
