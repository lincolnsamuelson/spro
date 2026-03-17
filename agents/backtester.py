from __future__ import annotations
import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType
from event_bus import EventBus


class Backtester:
    """Continuously backtests strategies against recent price history.
    Tests different rotation thresholds, leverage levels, and entry conditions
    to find what ACTUALLY works — then broadcasts winning configs to all agents."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("backtester", topics={
            EventType.PRICE_UPDATE, EventType.ORDER_FILLED, EventType.SHUTDOWN,
        })
        # Rolling price history per coin: deque of (timestamp, price)
        self.price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))
        self.tests_run: int = 0
        self.best_config: dict = {}
        self.best_win_rate: float = 0.0
        self.best_avg_pnl: float = 0.0
        self.last_test_time: float = 0.0
        self.recent_results: list[dict] = []

    async def run(self):
        test_task = asyncio.create_task(self._test_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                test_task.cancel()
                return
            if event.type == EventType.PRICE_UPDATE:
                sym = event.payload["symbol"]
                self.price_history[sym].append(
                    (event.payload["timestamp"], event.payload["price"]))

    async def _test_loop(self):
        """Run backtests every 30 seconds on collected price data."""
        await asyncio.sleep(60)  # Wait for price data to accumulate
        while True:
            await asyncio.sleep(30)
            await self._run_backtest()

    async def _run_backtest(self):
        """Test multiple strategy configs against recent price data."""
        # Need at least 2 minutes of data
        if not self.price_history:
            return

        configs_to_test = [
            {"profit_target": 0.01, "loss_limit": -0.05, "leverage": 30, "label": "tight_scalp"},
            {"profit_target": 0.02, "loss_limit": -0.08, "leverage": 30, "label": "balanced"},
            {"profit_target": 0.03, "loss_limit": -0.10, "leverage": 30, "label": "patient"},
            {"profit_target": 0.02, "loss_limit": -0.06, "leverage": 25, "label": "conservative"},
            {"profit_target": 0.015, "loss_limit": -0.05, "leverage": 35, "label": "aggressive_scalp"},
            {"profit_target": 0.03, "loss_limit": -0.03, "leverage": 30, "label": "symmetric"},
            {"profit_target": 0.04, "loss_limit": -0.12, "leverage": 30, "label": "wide"},
            {"profit_target": 0.02, "loss_limit": -0.15, "leverage": 30, "label": "current"},
        ]

        results = []
        for cfg in configs_to_test:
            result = self._simulate(cfg)
            results.append(result)

        self.tests_run += len(results)
        self.recent_results = results

        # Find the best config by score: win_rate * avg_pnl (both matter)
        best = max(results, key=lambda r: r["score"])
        if best["total_trades"] >= 5 and best["score"] > 0:
            self.best_config = best["config"]
            self.best_win_rate = best["win_rate"]
            self.best_avg_pnl = best["avg_pnl"]

            await self.bus.publish(Event(
                type=EventType.BACKTEST_RESULT,
                payload={
                    "best_config": best["config"],
                    "win_rate": best["win_rate"],
                    "avg_pnl": best["avg_pnl"],
                    "total_trades": best["total_trades"],
                    "total_pnl": best["total_pnl"],
                    "score": best["score"],
                    "tests_run": self.tests_run,
                    "all_results": [
                        {"label": r["config"]["label"], "win_rate": r["win_rate"],
                         "avg_pnl": round(r["avg_pnl"], 4), "trades": r["total_trades"],
                         "score": round(r["score"], 4)}
                        for r in results
                    ],
                },
                source="backtester",
            ))
        self.last_test_time = time.time()

    def _simulate(self, cfg: dict) -> dict:
        """Simulate a rotation strategy against collected price history."""
        profit_target = cfg["profit_target"]
        loss_limit = cfg["loss_limit"]
        leverage = cfg["leverage"]

        wins = 0
        losses = 0
        total_pnl = 0.0
        trades = 0

        for sym, prices in self.price_history.items():
            if len(prices) < 20:
                continue

            price_list = list(prices)
            # Simulate: enter at each point, check if profit or loss hit first
            step = max(1, len(price_list) // 50)  # Sample ~50 entry points per coin
            for i in range(0, len(price_list) - 5, step):
                entry_price = price_list[i][1]
                if entry_price <= 0:
                    continue

                # Walk forward to see what happens
                hit_profit = False
                hit_loss = False
                for j in range(i + 1, min(i + 200, len(price_list))):
                    future_price = price_list[j][1]
                    price_change = (future_price - entry_price) / entry_price
                    margin_return = price_change * leverage

                    if margin_return >= profit_target:
                        hit_profit = True
                        break
                    elif margin_return <= loss_limit:
                        hit_loss = True
                        break

                if hit_profit:
                    wins += 1
                    total_pnl += profit_target * 100  # normalized to $100 margin
                    trades += 1
                elif hit_loss:
                    losses += 1
                    total_pnl += loss_limit * 100
                    trades += 1
                # If neither hit, position still open — don't count

        win_rate = (wins / trades * 100) if trades > 0 else 0
        avg_pnl = (total_pnl / trades) if trades > 0 else 0
        # Score rewards both high win rate AND positive avg P&L
        score = (win_rate / 100) * max(avg_pnl, 0) if avg_pnl > 0 else avg_pnl * 0.01

        return {
            "config": cfg,
            "wins": wins,
            "losses": losses,
            "total_trades": trades,
            "win_rate": round(win_rate, 1),
            "avg_pnl": round(avg_pnl, 4),
            "total_pnl": round(total_pnl, 2),
            "score": round(score, 4),
        }

    def get_summary(self) -> dict:
        return {
            "tests_run": self.tests_run,
            "best_config": self.best_config,
            "best_win_rate": self.best_win_rate,
            "best_avg_pnl": round(self.best_avg_pnl, 4),
            "recent_results": [
                {"label": r["config"]["label"], "win_rate": r["win_rate"],
                 "avg_pnl": round(r["avg_pnl"], 4), "trades": r["total_trades"],
                 "score": round(r["score"], 4)}
                for r in self.recent_results
            ],
        }
