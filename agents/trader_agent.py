from __future__ import annotations
import asyncio
import time
from collections import defaultdict
from models import Event, EventType, Side
from event_bus import EventBus


# Strategy style configurations
STYLE_WEIGHTS = {
    "momentum_chaser": {
        "rsi": 0.10, "macd": 0.15, "bollinger": 0.10, "ema_crossover": 0.05,
        "sentiment": 0.02, "momentum": 0.35, "volatility": 0.15,
        "order_flow": 0.05, "correlation": 0.03,
    },
    "breakout_hunter": {
        "rsi": 0.10, "macd": 0.15, "bollinger": 0.15, "ema_crossover": 0.10,
        "sentiment": 0.03, "momentum": 0.25, "volatility": 0.15,
        "order_flow": 0.04, "correlation": 0.03,
    },
    "scalper": {
        "rsi": 0.15, "macd": 0.20, "bollinger": 0.15, "ema_crossover": 0.10,
        "sentiment": 0.02, "momentum": 0.20, "volatility": 0.10,
        "order_flow": 0.05, "correlation": 0.03,
    },
    "mean_reverter": {
        "rsi": 0.25, "macd": 0.15, "bollinger": 0.25, "ema_crossover": 0.10,
        "sentiment": 0.05, "momentum": 0.05, "volatility": 0.10,
        "order_flow": 0.03, "correlation": 0.02,
    },
    "sentiment_rider": {
        "rsi": 0.10, "macd": 0.10, "bollinger": 0.10, "ema_crossover": 0.05,
        "sentiment": 0.25, "momentum": 0.15, "volatility": 0.10,
        "order_flow": 0.05, "correlation": 0.10,
    },
}

STYLE_COOLDOWNS = {
    "momentum_chaser": 3,
    "breakout_hunter": 5,
    "scalper": 2,
    "mean_reverter": 10,
    "sentiment_rider": 8,
}

STYLE_THRESHOLDS = {
    "momentum_chaser": 0.25,
    "breakout_hunter": 0.28,
    "scalper": 0.22,
    "mean_reverter": 0.30,
    "sentiment_rider": 0.28,
}


class TraderAgent:
    """Independent trader with its own capital slice, strategy style, and
    decision-making. Each trader receives signals from the router and
    independently decides whether to trade."""

    def __init__(self, trader_id: str, bus: EventBus, config: dict,
                 portfolio_mgr, style: str, capital: float, max_positions: int):
        self.trader_id = trader_id
        self.bus = bus
        self.config = config
        self.portfolio_mgr = portfolio_mgr
        self.style = style
        self.capital = capital
        self.max_positions = max_positions
        self.signal_queue = asyncio.Queue(maxsize=2000)

        self.weights = dict(STYLE_WEIGHTS.get(style, STYLE_WEIGHTS["momentum_chaser"]))
        self.cooldown = STYLE_COOLDOWNS.get(style, 5)
        self.threshold = STYLE_THRESHOLDS.get(style, 0.28)

        # Signal accumulation
        self.signals: dict[str, dict] = defaultdict(dict)
        self.last_trade_signal: dict[str, float] = {}
        self.latest_prices: dict[str, float] = {}
        self.volatility_data: dict[str, dict] = {}

        # Track my positions and what I own
        self.my_positions: set = set()
        self.pending_symbols: set = set()
        self.compound_multiplier: float = 1.0

        # Learned intelligence
        self.blacklist: set = set()
        self.probation: set = set()
        self.leverage_overrides: dict[str, int] = {}
        self.bad_combos: dict[str, float] = {}

        # Stats
        self.signals_received: int = 0
        self.trades_sent: int = 0

        # Listen for portfolio updates and price updates
        self.portfolio_queue = bus.subscribe(
            f"trader_{trader_id}_portfolio",
            topics={EventType.PORTFOLIO_UPDATE, EventType.ORDER_FILLED,
                    EventType.PRICE_UPDATE, EventType.SHUTDOWN},
        )

    async def run(self):
        await asyncio.gather(
            self._signal_loop(),
            self._portfolio_loop(),
        )

    async def _portfolio_loop(self):
        while True:
            event = await self.portfolio_queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PORTFOLIO_UPDATE:
                self._on_portfolio_update(event.payload)
            elif event.type == EventType.ORDER_FILLED:
                self._on_order_filled(event.payload)
            elif event.type == EventType.PRICE_UPDATE:
                self.latest_prices[event.payload["symbol"]] = event.payload["price"]

    def _on_portfolio_update(self, payload: dict):
        # Update my view of which positions I own
        my_syms = set()
        for sym, pos_data in payload.get("positions", {}).items():
            if pos_data.get("trader_id") == self.trader_id:
                my_syms.add(sym)
        self.my_positions = my_syms

    def _on_order_filled(self, payload: dict):
        symbol = payload.get("symbol", "")
        trader_id = payload.get("trader_id", "")
        if trader_id == self.trader_id:
            self.pending_symbols.discard(symbol)
            if payload.get("side") == "BUY":
                self.my_positions.add(symbol)
            elif payload.get("side") == "SELL":
                self.my_positions.discard(symbol)

    async def _signal_loop(self):
        while True:
            event = await self.signal_queue.get()
            if event.type == EventType.SHUTDOWN:
                return

            self.signals_received += 1

            if event.type == EventType.STRATEGY_ADJUSTMENT:
                self._apply_adjustment(event.payload)
                continue

            if event.type == EventType.COMPOUND_TRIGGER:
                self.compound_multiplier = event.payload.get("multiplier", 1.0)
                await self._deploy_idle_cash()
                continue

            if event.type == EventType.PRICE_UPDATE:
                self.latest_prices[event.payload["symbol"]] = event.payload["price"]
                continue

            # Process researcher signal
            symbol = event.payload.get("symbol", "")
            if not symbol:
                continue

            indicator = event.payload.get("indicator", "")
            now = time.time()

            # Map event types to indicator names
            if event.type == EventType.MOMENTUM_SIGNAL:
                indicator = "momentum"
            elif event.type == EventType.VOLATILITY_RANKING:
                indicator = "volatility"
                self.volatility_data[symbol] = event.payload
            elif event.type == EventType.ORDER_FLOW_SIGNAL:
                indicator = "order_flow"
            elif event.type == EventType.CORRELATION_SIGNAL:
                indicator = "correlation"
            elif event.type == EventType.MICROSTRUCTURE_SIGNAL:
                indicator = indicator or "microstructure"

            if not indicator:
                continue

            self.signals[symbol][indicator] = (
                event.payload.get("direction", "buy"),
                event.payload.get("strength", 0.5),
                now,
            )

            # Momentum fast-track for momentum_chaser and breakout_hunter
            if (event.type == EventType.MOMENTUM_SIGNAL and
                    self.style in ("momentum_chaser", "breakout_hunter") and
                    event.payload.get("strength", 0) >= 0.6):
                last = self.last_trade_signal.get(symbol, 0)
                if now - last >= self.cooldown:
                    direction = Side.BUY if event.payload["direction"] == "buy" else Side.SELL
                    confidence = event.payload["strength"]
                    if self._is_on_probation(symbol):
                        confidence *= 0.6
                    confidence -= self._get_combo_penalty(symbol)
                    if confidence >= 0.2:
                        leverage = self._get_leverage(symbol)
                        await self._emit_trade(symbol, direction, confidence, leverage)
                        self.last_trade_signal[symbol] = now
                        return

            # Normal signal evaluation
            last = self.last_trade_signal.get(symbol, 0)
            if now - last < self.cooldown:
                continue

            await self._evaluate_signals(symbol, now)

    def _is_blocked(self, symbol: str) -> bool:
        return symbol in self.blacklist

    def _is_on_probation(self, symbol: str) -> bool:
        return symbol in self.probation

    def _get_leverage(self, symbol: str) -> int:
        if symbol in self.leverage_overrides:
            return self.leverage_overrides[symbol]
        return self.volatility_data.get(symbol, {}).get(
            "recommended_leverage", self.config["trading"]["default_leverage"])

    def _get_combo_penalty(self, symbol: str) -> float:
        penalty = 0.0
        for combo_key, neg_pnl in self.bad_combos.items():
            if combo_key.endswith(f":{symbol}"):
                penalty += min(abs(neg_pnl) * 0.05, 0.1)
        return min(penalty, 0.3)

    async def _evaluate_signals(self, symbol: str, now: float):
        if self._is_blocked(symbol):
            return

        buy_score = 0.0
        sell_score = 0.0
        total_weight = 0.0

        # Expire old signals
        expired = [k for k, v in self.signals[symbol].items() if now - v[2] > 60]
        for k in expired:
            del self.signals[symbol][k]

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

        if self._is_on_probation(symbol):
            buy_score *= 0.6
            sell_score *= 0.6

        combo_penalty = self._get_combo_penalty(symbol)
        buy_score -= combo_penalty
        sell_score -= combo_penalty

        leverage = self._get_leverage(symbol)

        if buy_score >= self.threshold:
            await self._emit_trade(symbol, Side.BUY, buy_score, leverage)
            self.last_trade_signal[symbol] = now
        elif sell_score >= self.threshold:
            await self._emit_trade(symbol, Side.SELL, sell_score, leverage)
            self.last_trade_signal[symbol] = now

    async def _emit_trade(self, symbol: str, direction: Side, confidence: float, leverage: int):
        price = self.latest_prices.get(symbol, 0)
        if price == 0:
            return

        if direction == Side.BUY:
            # Check if I already hold or am pending this symbol
            if symbol in self.my_positions or symbol in self.pending_symbols:
                return
            # Also check global positions to avoid duplicates across traders
            if symbol in self.portfolio_mgr.portfolio.positions:
                return
            if len(self.my_positions) + len(self.pending_symbols) >= self.max_positions:
                return

            # Position sizing: my capital slice / my max positions
            margin = self.capital / self.max_positions
            # But never more than available cash
            available = self.portfolio_mgr.portfolio.cash - 0.01
            margin = min(margin, available)
            if margin < 0.10:
                return

            stop_loss_pct = self.config["risk"]["default_stop_loss_pct"]
            stop_loss = price * (1 - stop_loss_pct)

            self.pending_symbols.add(symbol)
            self.trades_sent += 1

            await self.bus.publish(Event(
                type=EventType.ORDER_REQUEST,
                payload={
                    "symbol": symbol,
                    "side": direction.value,
                    "quantity": 0,  # Calculated by portfolio manager
                    "margin": round(margin, 4),
                    "price": price,
                    "confidence": round(confidence, 4),
                    "leverage": leverage,
                    "stop_loss": round(stop_loss, 6),
                    "trader_id": self.trader_id,
                },
                source=f"trader_{self.trader_id}",
            ))

        elif direction == Side.SELL:
            if symbol not in self.my_positions:
                return

            self.trades_sent += 1

            await self.bus.publish(Event(
                type=EventType.ORDER_REQUEST,
                payload={
                    "symbol": symbol,
                    "side": direction.value,
                    "quantity": 0,
                    "margin": 0,
                    "price": price,
                    "confidence": round(confidence, 4),
                    "leverage": 1,
                    "stop_loss": 0,
                    "trader_id": self.trader_id,
                },
                source=f"trader_{self.trader_id}",
            ))

    async def _deploy_idle_cash(self):
        """Deploy idle cash into best available coins."""
        now = time.time()
        available = self.portfolio_mgr.portfolio.cash - 0.01
        if available < 0.10:
            return

        # How many more positions can I take?
        current_count = len(self.my_positions) + len(self.pending_symbols)
        slots = self.max_positions - current_count
        if slots <= 0:
            return

        candidates = []
        for symbol, sigs in self.signals.items():
            if symbol in self.my_positions or symbol in self.pending_symbols:
                continue
            if symbol in self.portfolio_mgr.portfolio.positions:
                continue
            if self._is_blocked(symbol):
                continue
            if not self.latest_prices.get(symbol):
                continue

            active = {k: v for k, v in sigs.items() if now - v[2] < 300}
            if not active:
                continue

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
                if buy_score > 0.05:
                    leverage = self._get_leverage(symbol)
                    candidates.append((symbol, buy_score, leverage))

        if not candidates:
            # Fallback: any coin with price data
            for sym in self.latest_prices:
                if (sym not in self.my_positions and
                        sym not in self.pending_symbols and
                        sym not in self.portfolio_mgr.portfolio.positions and
                        not self._is_blocked(sym)):
                    lev = self._get_leverage(sym)
                    candidates.append((sym, 0.3, lev))
                    if len(candidates) >= slots:
                        break

        if not candidates:
            return

        candidates.sort(key=lambda x: x[1], reverse=True)
        deploy_count = min(len(candidates), slots)
        for symbol, score, leverage in candidates[:deploy_count]:
            await self._emit_trade(symbol, Side.BUY, max(score, 0.3), leverage)
            self.last_trade_signal[symbol] = now

    def _apply_adjustment(self, payload: dict):
        # Weight adjustments
        adjustments = payload.get("weight_adjustments", {})
        for ind, adj in adjustments.items():
            if ind in self.weights:
                self.weights[ind] = max(0.05, min(0.50, self.weights[ind] + adj))
        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: v / total for k, v in self.weights.items()}

        thresh_adj = payload.get("threshold_adjustment", 0)
        self.threshold = max(0.15, min(0.65, self.threshold + thresh_adj))

        self.blacklist = set(payload.get("blacklist", []))
        self.probation = set(payload.get("probation", []))
        self.leverage_overrides = payload.get("leverage_overrides", {})
        self.bad_combos = payload.get("bad_combos", {})
