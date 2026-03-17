from __future__ import annotations
import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType
from event_bus import EventBus


class RegimeDetector:
    """Detects market regime (trending up, trending down, ranging, volatile)
    and broadcasts to all agents so they adjust strategy accordingly.

    In trending markets: momentum strategies win, be aggressive
    In ranging markets: mean reversion wins, take quick profits
    In volatile markets: scalping wins, tight stops
    In dead markets: don't trade, save capital"""

    REGIMES = ["trending_up", "trending_down", "ranging", "volatile", "dead"]

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("regime_detector", topics={
            EventType.PRICE_UPDATE, EventType.SHUTDOWN,
        })
        # Per-coin price history
        self.price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        self.current_regime: str = "ranging"
        self.regime_confidence: float = 0.0
        self.regime_duration: float = 0.0
        self._regime_start: float = time.time()
        self.coin_regimes: dict[str, str] = {}
        self.regime_history: list[dict] = []
        self.detections_run: int = 0

        # Recommended strategy adjustments per regime
        self.regime_strategies = {
            "trending_up": {
                "best_style": "momentum_chaser",
                "profit_target_mult": 1.5,   # Let winners run
                "loss_limit_mult": 0.8,       # Tighter stops
                "threshold_mult": 0.8,        # More aggressive entries
                "leverage_mult": 1.1,
            },
            "trending_down": {
                "best_style": "mean_reverter",
                "profit_target_mult": 0.7,    # Quick profits on bounces
                "loss_limit_mult": 0.6,       # Very tight stops
                "threshold_mult": 1.3,        # Be pickier
                "leverage_mult": 0.7,
            },
            "ranging": {
                "best_style": "scalper",
                "profit_target_mult": 0.8,    # Quick in-and-out
                "loss_limit_mult": 1.0,       # Normal stops
                "threshold_mult": 1.0,        # Normal
                "leverage_mult": 1.0,
            },
            "volatile": {
                "best_style": "breakout_hunter",
                "profit_target_mult": 1.3,    # Big moves available
                "loss_limit_mult": 1.2,       # Wider stops for noise
                "threshold_mult": 0.9,        # Slightly aggressive
                "leverage_mult": 0.8,         # Lower leverage = survive
            },
            "dead": {
                "best_style": "scalper",
                "profit_target_mult": 0.5,    # Tiny profits
                "loss_limit_mult": 0.5,       # Tiny stops
                "threshold_mult": 1.5,        # Very picky — don't waste money
                "leverage_mult": 0.5,
            },
        }

    async def run(self):
        detect_task = asyncio.create_task(self._detect_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                detect_task.cancel()
                return
            if event.type == EventType.PRICE_UPDATE:
                sym = event.payload["symbol"]
                self.price_history[sym].append(
                    (event.payload["timestamp"], event.payload["price"]))

    async def _detect_loop(self):
        """Detect market regime every 10 seconds."""
        await asyncio.sleep(30)  # Wait for data
        while True:
            await asyncio.sleep(10)
            await self._detect_regime()

    async def _detect_regime(self):
        self.detections_run += 1
        now = time.time()

        # Analyze each coin's regime
        up_coins = 0
        down_coins = 0
        ranging_coins = 0
        volatile_coins = 0
        dead_coins = 0
        total_analyzed = 0

        for sym, prices in self.price_history.items():
            if len(prices) < 10:
                continue
            total_analyzed += 1
            regime = self._classify_coin(list(prices))
            self.coin_regimes[sym] = regime
            if regime == "trending_up":
                up_coins += 1
            elif regime == "trending_down":
                down_coins += 1
            elif regime == "volatile":
                volatile_coins += 1
            elif regime == "dead":
                dead_coins += 1
            else:
                ranging_coins += 1

        if total_analyzed == 0:
            return

        # Overall market regime = majority of coins
        regime_counts = {
            "trending_up": up_coins,
            "trending_down": down_coins,
            "ranging": ranging_coins,
            "volatile": volatile_coins,
            "dead": dead_coins,
        }
        new_regime = max(regime_counts, key=regime_counts.get)
        confidence = regime_counts[new_regime] / total_analyzed

        if new_regime != self.current_regime:
            self._regime_start = now
            self.regime_history.append({
                "regime": new_regime,
                "confidence": round(confidence, 2),
                "time": now,
            })
            if len(self.regime_history) > 50:
                self.regime_history = self.regime_history[-30:]

        self.current_regime = new_regime
        self.regime_confidence = confidence
        self.regime_duration = now - self._regime_start

        strategy = self.regime_strategies.get(new_regime, self.regime_strategies["ranging"])

        # Find coins matching the current regime's best opportunities
        hot_regime_coins = []
        for sym, r in self.coin_regimes.items():
            if new_regime == "trending_up" and r == "trending_up":
                hot_regime_coins.append(sym)
            elif new_regime == "volatile" and r == "volatile":
                hot_regime_coins.append(sym)
            elif new_regime == "ranging" and r in ("ranging", "volatile"):
                hot_regime_coins.append(sym)

        await self.bus.publish(Event(
            type=EventType.MARKET_REGIME,
            payload={
                "regime": new_regime,
                "confidence": round(confidence, 2),
                "duration_seconds": round(self.regime_duration, 0),
                "strategy": strategy,
                "coin_regimes": dict(list(self.coin_regimes.items())[:20]),
                "hot_regime_coins": hot_regime_coins[:10],
                "regime_counts": regime_counts,
                "detections_run": self.detections_run,
            },
            source="regime_detector",
        ))

    def _classify_coin(self, prices: list[tuple[float, float]]) -> str:
        """Classify a coin's current regime from its price history."""
        if len(prices) < 5:
            return "dead"

        recent = prices[-min(60, len(prices)):]
        p_values = [p for _, p in recent]

        if not p_values or p_values[0] == 0:
            return "dead"

        # Trend: compare start vs end
        change_pct = (p_values[-1] - p_values[0]) / p_values[0] * 100

        # Volatility: std dev of returns
        returns = []
        for i in range(1, len(p_values)):
            if p_values[i - 1] > 0:
                returns.append((p_values[i] - p_values[i - 1]) / p_values[i - 1] * 100)

        if not returns:
            return "dead"

        avg_return = sum(returns) / len(returns)
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        volatility = variance ** 0.5

        # Max move
        min_p = min(p_values)
        max_p = max(p_values)
        max_range = (max_p - min_p) / min_p * 100 if min_p > 0 else 0

        # Classification
        if max_range < 0.1:
            return "dead"
        elif volatility > 0.5 and abs(change_pct) < 0.3:
            return "volatile"  # High variance but no direction
        elif change_pct > 0.3 and volatility < 1.0:
            return "trending_up"
        elif change_pct < -0.3 and volatility < 1.0:
            return "trending_down"
        elif volatility > 0.8:
            return "volatile"
        else:
            return "ranging"

    def get_summary(self) -> dict:
        return {
            "current_regime": self.current_regime,
            "confidence": round(self.regime_confidence * 100, 1),
            "duration_seconds": round(self.regime_duration, 0),
            "detections_run": self.detections_run,
            "coin_regimes": dict(list(self.coin_regimes.items())[:15]),
            "strategy": self.regime_strategies.get(self.current_regime, {}),
            "recent_history": self.regime_history[-5:],
        }
