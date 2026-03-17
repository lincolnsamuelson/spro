import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType, Side
from event_bus import EventBus


class VolatilityScanner:
    """Scans all coins and ranks them by volatility. Identifies the hottest
    movers and emits volatility signals. High volatility = opportunity for
    leveraged trades."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("volatility")
        self.vol_cfg = config["volatility"]
        self.lookback = self.vol_cfg["lookback_periods"]
        self.min_vol = self.vol_cfg["min_volatility_pct"]
        self.top_n = self.vol_cfg["top_n_volatile"]
        # Price history per symbol
        self.price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        self.volatility_scores: dict[str, float] = {}
        self.hot_coins: list[str] = []

    async def run(self):
        rank_task = asyncio.create_task(self._rank_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                rank_task.cancel()
                return
            if event.type == EventType.PRICE_UPDATE:
                symbol = event.payload["symbol"]
                price = event.payload["price"]
                self.price_history[symbol].append((event.payload["timestamp"], price))

    async def _rank_loop(self):
        """Rank coins by volatility every 30 seconds."""
        while True:
            await asyncio.sleep(30)
            await self._compute_rankings()

    async def _compute_rankings(self):
        scores = {}
        for symbol, history in self.price_history.items():
            if len(history) < 3:
                continue
            prices = [p for _, p in history]
            recent = prices[-self.lookback:] if len(prices) >= self.lookback else prices

            # Calculate volatility: standard deviation of returns
            if len(recent) < 2:
                continue
            returns = [(recent[i] - recent[i-1]) / recent[i-1] * 100
                       for i in range(1, len(recent)) if recent[i-1] > 0]
            if not returns:
                continue
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
            volatility = variance ** 0.5

            # Also compute price range as % of current price
            price_range = (max(recent) - min(recent)) / min(recent) * 100 if min(recent) > 0 else 0

            # Combined score: volatility + range
            score = volatility * 0.6 + price_range * 0.4
            scores[symbol] = score

        self.volatility_scores = scores

        # Rank and pick top N
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        self.hot_coins = [sym for sym, _ in ranked[:self.top_n]]

        # Emit signals for hot coins
        for sym, score in ranked[:self.top_n]:
            if score < self.min_vol:
                continue
            # Determine direction from recent trend
            history = self.price_history.get(sym, deque())
            if len(history) < 3:
                continue
            prices = [p for _, p in history]
            recent_trend = (prices[-1] - prices[-3]) / prices[-3] if prices[-3] > 0 else 0

            direction = Side.BUY if recent_trend > 0 else Side.SELL
            strength = min(score / 5.0, 1.0)  # Normalize to 0-1

            # Recommend higher leverage for higher volatility
            leverage = min(int(score * 3) + 5, self.config["trading"]["max_leverage"])

            await self.bus.publish(Event(
                type=EventType.VOLATILITY_RANKING,
                payload={
                    "symbol": sym,
                    "indicator": "volatility",
                    "direction": direction.value,
                    "strength": round(strength, 3),
                    "volatility_score": round(score, 3),
                    "recommended_leverage": leverage,
                    "price_range_pct": round((max(prices[-10:]) - min(prices[-10:])) / min(prices[-10:]) * 100, 3) if len(prices) >= 10 and min(prices[-10:]) > 0 else 0,
                },
                source="volatility",
            ))

        # Also publish full ranking for dashboard
        await self.bus.publish(Event(
            type=EventType.HEARTBEAT,
            payload={
                "hot_coins": self.hot_coins[:10],
                "scores": {s: round(v, 3) for s, v in ranked[:10]},
            },
            source="volatility",
        ))

    def get_leverage_recommendation(self, symbol: str) -> int:
        """Get recommended leverage for a symbol based on volatility."""
        score = self.volatility_scores.get(symbol, 0)
        if score > 3:
            return self.config["trading"]["max_leverage"]
        elif score > 1.5:
            return self.config["trading"]["default_leverage"]
        else:
            return max(5, self.config["trading"]["default_leverage"] // 2)
