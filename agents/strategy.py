import asyncio
import time
from collections import defaultdict
from models import Event, EventType, Side
from event_bus import EventBus


class StrategyEngine:
    """Aggressive strategy engine. Combines signals from all analysts including
    momentum and volatility. Lower thresholds, faster action. Momentum signals
    get priority — when a breakout fires, act immediately."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("strategy")
        self.strat_cfg = config["strategy"]
        self.weights = dict(self.strat_cfg["weights"])
        self.threshold = self.strat_cfg["signal_threshold"]
        self.cooldown = self.strat_cfg["cooldown_seconds"]
        self.signals: dict[str, dict] = defaultdict(dict)
        self.last_trade_signal: dict[str, float] = {}
        self.latest_prices: dict[str, float] = {}
        self.volatility_data: dict[str, dict] = {}
        self.compound_multiplier: float = 1.0
        self.adjustments_applied: int = 0
        self.held_symbols: set = set()

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PRICE_UPDATE:
                self.latest_prices[event.payload["symbol"]] = event.payload["price"]
            elif event.type in (EventType.TECHNICAL_SIGNAL, EventType.SENTIMENT_SIGNAL):
                await self._on_signal(event.payload)
            elif event.type == EventType.MOMENTUM_SIGNAL:
                await self._on_momentum(event.payload)
            elif event.type == EventType.VOLATILITY_RANKING:
                self._on_volatility(event.payload)
            elif event.type == EventType.STRATEGY_ADJUSTMENT:
                self._apply_adjustment(event.payload)
            elif event.type == EventType.COMPOUND_TRIGGER:
                self.compound_multiplier = event.payload.get("multiplier", 1.0)
                await self._deploy_idle_cash(event.payload)
            elif event.type == EventType.PORTFOLIO_UPDATE:
                self.held_symbols = set(event.payload.get("position_symbols", []))

    def _on_volatility(self, payload: dict):
        symbol = payload["symbol"]
        self.volatility_data[symbol] = payload
        # Treat volatility ranking as a signal too
        self.signals[symbol]["volatility"] = (
            payload["direction"],
            payload["strength"],
            time.time(),
        )

    async def _on_momentum(self, payload: dict):
        """Momentum gets special treatment — it can trigger trades with reduced cooldown."""
        symbol = payload["symbol"]
        now = time.time()

        # Store as regular signal
        self.signals[symbol]["momentum"] = (
            payload["direction"],
            payload["strength"],
            now,
        )

        # Momentum signals use half cooldown — speed matters
        last = self.last_trade_signal.get(symbol, 0)
        if now - last < self.cooldown / 2:
            return

        # If momentum is strong enough, trigger trade directly
        if payload["strength"] >= 0.6:
            direction = Side.BUY if payload["direction"] == "buy" else Side.SELL
            leverage = self.volatility_data.get(symbol, {}).get("recommended_leverage",
                        self.config["trading"]["default_leverage"])
            await self._emit_trade_signal(symbol, direction, payload["strength"], leverage)
            self.last_trade_signal[symbol] = now
            return

        # Otherwise, run normal weighted evaluation
        await self._evaluate_signals(symbol, now)

    async def _on_signal(self, payload: dict):
        symbol = payload["symbol"]
        indicator = payload["indicator"]
        now = time.time()

        self.signals[symbol][indicator] = (
            payload["direction"],
            payload["strength"],
            now,
        )

        # Expire old signals
        expired = [k for k, v in self.signals[symbol].items() if now - v[2] > 90]
        for k in expired:
            del self.signals[symbol][k]

        # Check cooldown
        if symbol in self.last_trade_signal:
            if now - self.last_trade_signal[symbol] < self.cooldown:
                return

        await self._evaluate_signals(symbol, now)

    async def _evaluate_signals(self, symbol: str, now: float):
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

        leverage = self.volatility_data.get(symbol, {}).get("recommended_leverage",
                    self.config["trading"]["default_leverage"])

        if buy_score >= self.threshold:
            await self._emit_trade_signal(symbol, Side.BUY, buy_score, leverage)
            self.last_trade_signal[symbol] = now
        elif sell_score >= self.threshold:
            await self._emit_trade_signal(symbol, Side.SELL, sell_score, leverage)
            self.last_trade_signal[symbol] = now

    async def _emit_trade_signal(self, symbol: str, direction: Side, confidence: float, leverage: int):
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
                "leverage": leverage,
                "multiplier": round(self.compound_multiplier, 2),
            },
            source="strategy",
        ))

    async def _deploy_idle_cash(self, payload: dict):
        """When compounder detects idle cash, scan ALL coins and pick the best
        one to deploy into immediately. No waiting for signals."""
        now = time.time()

        # Score every coin we have signals for but don't already hold
        candidates = []
        for symbol, sigs in self.signals.items():
            if symbol in self.held_symbols:
                continue
            if not self.latest_prices.get(symbol):
                continue

            # Expire old signals
            active = {k: v for k, v in sigs.items() if now - v[2] < 120}
            if not active:
                continue

            # Calculate buy score
            buy_score = 0.0
            total_weight = 0.0
            for ind, weight in self.weights.items():
                if ind in active:
                    d, s, _ = active[ind]
                    total_weight += weight
                    if d == Side.BUY.value:
                        buy_score += weight * s

            if total_weight > 0:
                buy_score /= total_weight
                leverage = self.volatility_data.get(symbol, {}).get(
                    "recommended_leverage", self.config["trading"]["default_leverage"])
                candidates.append((symbol, buy_score, leverage))

        if not candidates:
            # No signals at all? Pick the highest volatility coin we don't hold
            for sym in self.volatility_data:
                if sym not in self.held_symbols and sym in self.latest_prices:
                    lev = self.volatility_data[sym].get("recommended_leverage",
                            self.config["trading"]["default_leverage"])
                    candidates.append((sym, 0.5, lev))
                    break

        if not candidates:
            return

        # Pick the best candidate
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_symbol, best_score, best_leverage = candidates[0]

        # Deploy with minimum threshold of 0.2 (very aggressive)
        if best_score >= 0.2:
            await self._emit_trade_signal(best_symbol, Side.BUY, max(best_score, 0.5), best_leverage)
            self.last_trade_signal[best_symbol] = now

    def _apply_adjustment(self, payload: dict):
        adjustments = payload.get("weight_adjustments", {})
        for ind, adj in adjustments.items():
            if ind in self.weights:
                self.weights[ind] = max(0.05, min(0.50, self.weights[ind] + adj))
        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: v / total for k, v in self.weights.items()}

        thresh_adj = payload.get("threshold_adjustment", 0)
        self.threshold = max(0.25, min(0.65, self.threshold + thresh_adj))
        self.adjustments_applied += 1
