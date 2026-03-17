import asyncio
import time
from collections import defaultdict
from models import Event, EventType, Side
from event_bus import EventBus


class StrategyEngine:
    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("strategy")
        self.strat_cfg = config["strategy"]
        self.weights = dict(self.strat_cfg["weights"])  # mutable copy
        self.threshold = self.strat_cfg["signal_threshold"]
        self.cooldown = self.strat_cfg["cooldown_seconds"]
        # Per-symbol signal buffer: {symbol: {indicator: (direction, strength, timestamp)}}
        self.signals: dict[str, dict] = defaultdict(dict)
        self.last_trade_signal: dict[str, float] = {}
        self.latest_prices: dict[str, float] = {}
        self.adjustments_applied: int = 0

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PRICE_UPDATE:
                self.latest_prices[event.payload["symbol"]] = event.payload["price"]
            elif event.type in (EventType.TECHNICAL_SIGNAL, EventType.SENTIMENT_SIGNAL):
                await self._on_signal(event.payload)
            elif event.type == EventType.STRATEGY_ADJUSTMENT:
                self._apply_adjustment(event.payload)

    def _apply_adjustment(self, payload: dict):
        """Apply weight and threshold adjustments from the evaluator."""
        adjustments = payload.get("weight_adjustments", {})
        for ind, adj in adjustments.items():
            if ind in self.weights:
                self.weights[ind] = max(0.05, min(0.50, self.weights[ind] + adj))
        # Normalize weights to sum to 1.0
        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: v / total for k, v in self.weights.items()}

        thresh_adj = payload.get("threshold_adjustment", 0)
        self.threshold = max(0.35, min(0.75, self.threshold + thresh_adj))
        self.adjustments_applied += 1

    async def _on_signal(self, payload: dict):
        symbol = payload["symbol"]
        indicator = payload["indicator"]
        direction = payload["direction"]
        strength = payload["strength"]
        now = time.time()

        self.signals[symbol][indicator] = (direction, strength, now)

        # Expire signals older than 120 seconds
        expired = [k for k, v in self.signals[symbol].items() if now - v[2] > 120]
        for k in expired:
            del self.signals[symbol][k]

        # Check cooldown
        if symbol in self.last_trade_signal:
            if now - self.last_trade_signal[symbol] < self.cooldown:
                return

        # Weighted vote
        buy_score = 0.0
        sell_score = 0.0
        total_weight = 0.0

        for ind, weight in self.weights.items():
            if ind in self.signals[symbol]:
                d, s, _ = self.signals[symbol][ind]
                total_weight += weight
                if d == Side.BUY.value:
                    buy_score += weight * s
                else:
                    sell_score += weight * s

        if total_weight == 0:
            return

        buy_score /= total_weight
        sell_score /= total_weight

        if buy_score >= self.threshold:
            await self._emit_trade_signal(symbol, Side.BUY, buy_score)
            self.last_trade_signal[symbol] = now
        elif sell_score >= self.threshold:
            await self._emit_trade_signal(symbol, Side.SELL, sell_score)
            self.last_trade_signal[symbol] = now

    async def _emit_trade_signal(self, symbol: str, direction: Side, confidence: float):
        price = self.latest_prices.get(symbol, 0)
        if price == 0:
            return

        await self.bus.publish(Event(
            type=EventType.TRADE_SIGNAL,
            payload={
                "symbol": symbol,
                "direction": direction.value,
                "confidence": round(confidence, 4),
                "price": price,
            },
            source="strategy",
        ))
