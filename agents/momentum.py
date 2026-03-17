import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType, Side
from event_bus import EventBus


class MomentumDetector:
    """Detects breakouts, rapid price acceleration, and momentum surges.
    This is the primary signal generator for high-risk trades — when momentum
    fires, the strategy engine should act immediately."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("momentum")
        self.mom_cfg = config["momentum"]
        self.breakout_threshold = self.mom_cfg["breakout_threshold_pct"]
        self.accel_periods = self.mom_cfg["acceleration_periods"]
        # Price tracking
        self.price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.candle_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        # Track recent highs/lows for breakout detection
        self.recent_highs: dict[str, float] = {}
        self.recent_lows: dict[str, float] = {}
        # Active breakouts (to avoid repeated signals)
        self.active_breakouts: dict[str, float] = {}

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PRICE_UPDATE:
                await self._on_price(event.payload)
            elif event.type == EventType.CANDLE:
                self._on_candle(event.payload)

    def _on_candle(self, payload: dict):
        symbol = payload["symbol"]
        self.candle_history[symbol].append(payload)

        # Update recent highs/lows from candle data
        candles = list(self.candle_history[symbol])
        if len(candles) >= 10:
            recent = candles[-20:] if len(candles) >= 20 else candles
            self.recent_highs[symbol] = max(c["high"] for c in recent)
            self.recent_lows[symbol] = min(c["low"] for c in recent)

    async def _on_price(self, payload: dict):
        symbol = payload["symbol"]
        price = payload["price"]
        now = payload["timestamp"]
        self.price_history[symbol].append((now, price))

        prices = list(self.price_history[symbol])
        if len(prices) < 5:
            return

        # Check for breakout
        await self._check_breakout(symbol, price, now)

        # Check for momentum acceleration
        await self._check_acceleration(symbol, prices, now)

        # Check for rapid price spike
        await self._check_spike(symbol, prices, now)

    async def _check_breakout(self, symbol: str, price: float, now: float):
        """Detect price breaking above recent high or below recent low."""
        high = self.recent_highs.get(symbol)
        low = self.recent_lows.get(symbol)
        if high is None or low is None:
            return

        # Cooldown: don't signal same breakout within 2 minutes
        if symbol in self.active_breakouts:
            if now - self.active_breakouts[symbol] < 120:
                return

        range_pct = (high - low) / low * 100 if low > 0 else 0

        if price > high and range_pct > 0.3:
            breakout_strength = min((price - high) / high * 100 / self.breakout_threshold, 1.0)
            await self._emit(symbol, "breakout_high", Side.BUY, max(breakout_strength, 0.6), now)
            self.active_breakouts[symbol] = now

        elif price < low and range_pct > 0.3:
            breakout_strength = min((low - price) / low * 100 / self.breakout_threshold, 1.0)
            await self._emit(symbol, "breakout_low", Side.SELL, max(breakout_strength, 0.6), now)
            self.active_breakouts[symbol] = now

    async def _check_acceleration(self, symbol: str, prices: list, now: float):
        """Detect accelerating price movement — each interval moves more than the last."""
        if len(prices) < self.accel_periods + 1:
            return

        recent = prices[-(self.accel_periods + 1):]
        deltas = []
        for i in range(1, len(recent)):
            _, p1 = recent[i - 1]
            _, p2 = recent[i]
            if p1 > 0:
                deltas.append((p2 - p1) / p1 * 100)

        if len(deltas) < 3:
            return

        # Check if all deltas are in same direction and increasing
        all_positive = all(d > 0 for d in deltas)
        all_negative = all(d < 0 for d in deltas)
        increasing_magnitude = all(abs(deltas[i]) > abs(deltas[i-1]) for i in range(1, len(deltas)))

        if (all_positive or all_negative) and increasing_magnitude:
            total_move = sum(abs(d) for d in deltas)
            strength = min(total_move / 3.0, 1.0)
            direction = Side.BUY if all_positive else Side.SELL
            await self._emit(symbol, "acceleration", direction, max(strength, 0.5), now)

    async def _check_spike(self, symbol: str, prices: list, now: float):
        """Detect sudden price spike (>1% in last few ticks)."""
        if len(prices) < 3:
            return

        # Compare current price to price 3 ticks ago
        _, old_price = prices[-3]
        _, new_price = prices[-1]
        if old_price <= 0:
            return

        change_pct = (new_price - old_price) / old_price * 100

        if abs(change_pct) >= self.breakout_threshold:
            direction = Side.BUY if change_pct > 0 else Side.SELL
            strength = min(abs(change_pct) / 3.0, 1.0)
            await self._emit(symbol, "spike", direction, max(strength, 0.7), now)

    async def _emit(self, symbol: str, signal_type: str, direction: Side, strength: float, now: float):
        await self.bus.publish(Event(
            type=EventType.MOMENTUM_SIGNAL,
            payload={
                "symbol": symbol,
                "indicator": "momentum",
                "signal_type": signal_type,
                "direction": direction.value,
                "strength": round(strength, 3),
            },
            source="momentum",
        ))
