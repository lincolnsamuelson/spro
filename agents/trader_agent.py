from __future__ import annotations
import asyncio
import time
import hashlib
from collections import defaultdict
from models import Event, EventType, Side
from event_bus import EventBus


# Strategy style configurations — more differentiated
STYLE_WEIGHTS = {
    "momentum_chaser": {
        "rsi": 0.05, "macd": 0.10, "bollinger": 0.05, "ema_crossover": 0.03,
        "sentiment": 0.02, "momentum": 0.45, "volatility": 0.20,
        "order_flow": 0.05, "correlation": 0.05,
    },
    "breakout_hunter": {
        "rsi": 0.05, "macd": 0.20, "bollinger": 0.20, "ema_crossover": 0.15,
        "sentiment": 0.02, "momentum": 0.15, "volatility": 0.10,
        "order_flow": 0.08, "correlation": 0.05,
    },
    "scalper": {
        "rsi": 0.20, "macd": 0.25, "bollinger": 0.10, "ema_crossover": 0.15,
        "sentiment": 0.01, "momentum": 0.15, "volatility": 0.05,
        "order_flow": 0.06, "correlation": 0.03,
    },
    "mean_reverter": {
        "rsi": 0.35, "macd": 0.10, "bollinger": 0.30, "ema_crossover": 0.05,
        "sentiment": 0.05, "momentum": 0.02, "volatility": 0.05,
        "order_flow": 0.03, "correlation": 0.05,
    },
    "sentiment_rider": {
        "rsi": 0.05, "macd": 0.08, "bollinger": 0.08, "ema_crossover": 0.04,
        "sentiment": 0.35, "momentum": 0.10, "volatility": 0.10,
        "order_flow": 0.05, "correlation": 0.15,
    },
}

STYLE_COOLDOWNS = {
    "momentum_chaser": 3,
    "breakout_hunter": 8,
    "scalper": 1,
    "mean_reverter": 15,
    "sentiment_rider": 10,
}

STYLE_THRESHOLDS = {
    "momentum_chaser": 0.22,
    "breakout_hunter": 0.30,
    "scalper": 0.18,
    "mean_reverter": 0.35,
    "sentiment_rider": 0.28,
}


class TraderAgent:
    """Competitive trader that watches rivals, avoids herding, and
    learns from others' losses to make independent decisions."""

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

        # Coin preference seed — each trader naturally prefers different coins
        # based on hash of their ID, so they don't all pile into the same ones
        self._coin_seed = int(hashlib.md5(trader_id.encode()).hexdigest()[:8], 16)

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

        # Listen for portfolio updates, price updates, and firings
        self.portfolio_queue = bus.subscribe(
            f"trader_{trader_id}_portfolio",
            topics={EventType.PORTFOLIO_UPDATE, EventType.ORDER_FILLED,
                    EventType.PRICE_UPDATE, EventType.TRADER_FIRED,
                    EventType.SHUTDOWN},
        )

    async def run(self):
        await asyncio.gather(
            self._signal_loop(),
            self._portfolio_loop(),
            self._idle_cash_loop(),
        )

    async def _idle_cash_loop(self):
        """Continuously check for idle cash and deploy it immediately."""
        while True:
            await asyncio.sleep(2)
            await self._deploy_idle_cash()

    async def _portfolio_loop(self):
        while True:
            event = await self.portfolio_queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.TRADER_FIRED:
                if event.payload.get("trader_id") == self.trader_id:
                    self._on_fired(event.payload)
                continue
            if event.type == EventType.PORTFOLIO_UPDATE:
                self._on_portfolio_update(event.payload)
            elif event.type == EventType.ORDER_FILLED:
                self._on_order_filled(event.payload)
            elif event.type == EventType.PRICE_UPDATE:
                self.latest_prices[event.payload["symbol"]] = event.payload["price"]

    def _on_fired(self, payload: dict):
        """I got FIRED. Reset my brain, apply all learnings, start fresh."""
        self.signals.clear()
        self.last_trade_signal.clear()
        self.my_positions.clear()
        self.pending_symbols.clear()
        self.compound_multiplier = 1.0
        self.trades_sent = 0
        self.signals_received = 0
        self.weights = dict(STYLE_WEIGHTS.get(self.style, STYLE_WEIGHTS["momentum_chaser"]))
        self.cooldown = STYLE_COOLDOWNS.get(self.style, 5)
        self.threshold = STYLE_THRESHOLDS.get(self.style, 0.28)

    def _on_portfolio_update(self, payload: dict):
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

    # ─── COMPETITIVE INTELLIGENCE ───

    def _get_rival_intel(self, symbol: str) -> dict:
        """Look at what other traders are doing with this coin."""
        holders = 0          # how many rivals hold this coin
        losing_holders = 0   # how many of those are losing money on it
        winning_holders = 0  # how many are profiting
        worst_rival_pnl = 0.0
        best_rival_pnl = 0.0
        losing_trader_count = 0  # how many rivals are losing overall (not just this coin)

        for tid, t_state in self.portfolio_mgr.traders.items():
            if tid == self.trader_id:
                continue
            # Is this rival losing overall?
            if t_state.total_value < t_state.starting_cash * 0.98:
                losing_trader_count += 1
            # Does this rival hold the coin?
            if symbol in t_state.positions:
                holders += 1
                pos = t_state.positions[symbol]
                if pos.unrealized_pnl > 0:
                    winning_holders += 1
                    best_rival_pnl = max(best_rival_pnl, pos.unrealized_pnl)
                else:
                    losing_holders += 1
                    worst_rival_pnl = min(worst_rival_pnl, pos.unrealized_pnl)

        return {
            "holders": holders,
            "losing_holders": losing_holders,
            "winning_holders": winning_holders,
            "worst_rival_pnl": worst_rival_pnl,
            "best_rival_pnl": best_rival_pnl,
            "losing_trader_count": losing_trader_count,
        }

    def _coin_affinity(self, symbol: str) -> float:
        """Each trader has a unique preference for different coins based on
        their ID hash. Returns 0.0-1.0 affinity score."""
        sym_hash = int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16)
        combined = (self._coin_seed ^ sym_hash) % 1000
        return combined / 1000.0

    def _competitive_score_adjust(self, symbol: str, base_score: float) -> float:
        """Adjust a signal score based on competitive intelligence."""
        intel = self._get_rival_intel(symbol)
        score = base_score

        # ANTI-HERDING: If 2+ rivals already hold this coin, big penalty
        # Don't pile in where everyone else already is
        if intel["holders"] >= 2:
            score -= 0.15 * intel["holders"]

        # AVOID LOSERS' PICKS: If losing rivals hold this coin, stay away
        if intel["losing_holders"] > 0:
            score -= 0.10 * intel["losing_holders"]

        # CONTRARIAN BONUS: If a coin is being held by losers with bad P&L,
        # the mean_reverter might actually want to short it
        if self.style == "mean_reverter" and intel["losing_holders"] >= 2:
            score += 0.05  # slight contrarian interest

        # WINNER AWARENESS: If the top-performing rival is profiting on this
        # coin, small boost (follow the leader, but not too aggressively)
        if intel["winning_holders"] > 0 and intel["losing_holders"] == 0:
            score += 0.05

        # COIN AFFINITY: Natural preference based on trader identity
        # This ensures traders gravitate to different coins
        affinity = self._coin_affinity(symbol)
        if affinity > 0.6:
            score += 0.08  # prefer this coin
        elif affinity < 0.3:
            score -= 0.08  # avoid this coin (let others have it)

        return score

    # ─── SIGNAL PROCESSING ───

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

            symbol = event.payload.get("symbol", "")
            if not symbol:
                continue

            indicator = event.payload.get("indicator", "")
            now = time.time()

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

            # Momentum fast-track for momentum styles only
            if (event.type == EventType.MOMENTUM_SIGNAL and
                    self.style == "momentum_chaser" and
                    event.payload.get("strength", 0) >= 0.65):
                last = self.last_trade_signal.get(symbol, 0)
                if now - last >= self.cooldown:
                    direction = Side.BUY if event.payload["direction"] == "buy" else Side.SELL
                    confidence = event.payload["strength"]
                    confidence = self._competitive_score_adjust(symbol, confidence)
                    if self._is_on_probation(symbol):
                        confidence *= 0.6
                    confidence -= self._get_combo_penalty(symbol)
                    if confidence >= self.threshold:
                        leverage = self._get_leverage(symbol)
                        await self._emit_trade(symbol, direction, confidence, leverage)
                        self.last_trade_signal[symbol] = now

            # Normal evaluation
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

        # Apply competitive intelligence
        buy_score = self._competitive_score_adjust(symbol, buy_score)
        sell_score = self._competitive_score_adjust(symbol, sell_score)

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
            if symbol in self.my_positions or symbol in self.pending_symbols:
                return
            if len(self.my_positions) + len(self.pending_symbols) >= self.max_positions:
                return

            my_state = self.portfolio_mgr.traders.get(self.trader_id)
            if not my_state:
                return
            available = my_state.cash - 0.01
            margin = available / max(self.max_positions - len(self.my_positions) - len(self.pending_symbols), 1)
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
                    "quantity": 0,
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
        """Deploy idle cash — NEVER hold cash. Use competitive intel to pick coins."""
        now = time.time()
        my_state = self.portfolio_mgr.traders.get(self.trader_id)
        if not my_state:
            return
        available = my_state.cash - 0.01
        if available < 0.05:
            return

        current_count = len(self.my_positions) + len(self.pending_symbols)
        slots = self.max_positions - current_count
        if slots <= 0:
            return

        candidates = []
        for symbol, sigs in self.signals.items():
            if symbol in self.my_positions or symbol in self.pending_symbols:
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
                # Apply competitive intelligence
                buy_score = self._competitive_score_adjust(symbol, buy_score)
                if buy_score > 0.05:
                    leverage = self._get_leverage(symbol)
                    candidates.append((symbol, buy_score, leverage))

        if not candidates:
            # Fallback: coins with price data, but prefer MY coins (affinity)
            all_syms = list(self.latest_prices.keys())
            # Sort by affinity so each trader picks different fallback coins
            all_syms.sort(key=lambda s: self._coin_affinity(s), reverse=True)
            for sym in all_syms:
                if (sym not in self.my_positions and
                        sym not in self.pending_symbols and
                        not self._is_blocked(sym)):
                    intel = self._get_rival_intel(sym)
                    # Skip coins where 2+ rivals already are
                    if intel["holders"] >= 2:
                        continue
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
