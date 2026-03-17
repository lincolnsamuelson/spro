import asyncio
import time
from collections import defaultdict, deque
from models import Event, EventType, Side
from event_bus import EventBus


class Evaluator:
    """Evaluates each completed trade against all possible trades at the same time.
    Tracks which coins moved the most during the trade window and compares our
    actual trade performance against what we could have earned. Feeds adjustments
    back into the strategy engine to refine weights and thresholds."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("evaluator")
        # Price snapshots: {symbol: deque of (timestamp, price)}
        self.price_snapshots: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        # Completed trades for evaluation
        self.completed_trades: list[dict] = []
        # Performance by indicator: tracks which indicators led to winning trades
        self.indicator_performance: dict[str, dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0, "trade_count": 0
        })
        # Performance by coin
        self.coin_performance: dict[str, dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0
        })
        # Best possible trades we missed
        self.missed_opportunities: list[dict] = []
        # Current strategy adjustments (published to strategy engine)
        self.weight_adjustments: dict[str, float] = {}
        self.threshold_adjustment: float = 0.0
        # Track signals that led to each trade
        self.trade_signals: dict[str, list] = {}  # symbol -> signals at trade time
        self.recent_signals: dict[str, dict] = defaultdict(dict)
        # Evaluation stats
        self.evaluations_run: int = 0
        self.avg_efficiency: float = 0.0  # our P&L vs best possible

    async def run(self):
        eval_task = asyncio.create_task(self._periodic_eval())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                eval_task.cancel()
                return
            if event.type == EventType.PRICE_UPDATE:
                self._record_price(event.payload)
            elif event.type == EventType.ORDER_FILLED:
                self._on_trade(event.payload)
            elif event.type in (EventType.TECHNICAL_SIGNAL, EventType.SENTIMENT_SIGNAL):
                self._track_signal(event.payload)

    def _record_price(self, payload: dict):
        symbol = payload["symbol"]
        self.price_snapshots[symbol].append((payload["timestamp"], payload["price"]))

    def _track_signal(self, payload: dict):
        symbol = payload.get("symbol", "")
        indicator = payload.get("indicator", "")
        self.recent_signals[symbol][indicator] = {
            "direction": payload.get("direction"),
            "strength": payload.get("strength", 0),
            "timestamp": time.time(),
        }

    def _on_trade(self, payload: dict):
        trade = dict(payload)
        symbol = trade.get("symbol", "")
        # Attach the signals that were active when this trade happened
        trade["active_signals"] = dict(self.recent_signals.get(symbol, {}))
        self.completed_trades.append(trade)

        # Track P&L by coin
        pnl = trade.get("pnl")
        if pnl is not None:
            perf = self.coin_performance[symbol]
            perf["total_pnl"] += pnl
            if pnl > 0:
                perf["wins"] += 1
            else:
                perf["losses"] += 1

            # Attribute P&L to indicators that contributed
            for ind, sig in trade.get("active_signals", {}).items():
                ip = self.indicator_performance[ind]
                ip["trade_count"] += 1
                ip["total_pnl"] += pnl
                if pnl > 0:
                    ip["wins"] += 1
                else:
                    ip["losses"] += 1

    async def _periodic_eval(self):
        """Run evaluation every 2 minutes."""
        while True:
            await asyncio.sleep(120)
            await self._evaluate()

    async def _evaluate(self):
        """Compare our trades against the best possible trades in the same window."""
        self.evaluations_run += 1
        now = time.time()

        # Find the best moves across all coins in the last 5 minutes
        window = 300  # 5 min lookback
        best_moves = []
        for symbol, snapshots in self.price_snapshots.items():
            prices_in_window = [(t, p) for t, p in snapshots if now - t <= window]
            if len(prices_in_window) < 2:
                continue
            min_price = min(p for _, p in prices_in_window)
            max_price = max(p for _, p in prices_in_window)
            if min_price > 0:
                move_pct = (max_price - min_price) / min_price * 100
                best_moves.append({
                    "symbol": symbol,
                    "move_pct": round(move_pct, 3),
                    "low": min_price,
                    "high": max_price,
                })

        best_moves.sort(key=lambda x: x["move_pct"], reverse=True)

        # Track missed opportunities (top movers we didn't trade)
        traded_symbols = {t["symbol"] for t in self.completed_trades if now - t.get("time", 0) <= window}
        self.missed_opportunities = [m for m in best_moves[:10] if m["symbol"] not in traded_symbols]

        # Calculate efficiency: our actual returns vs theoretical best
        our_recent_pnl = sum(
            t.get("pnl", 0) for t in self.completed_trades
            if t.get("pnl") is not None and now - t.get("time", 0) <= window
        )
        best_theoretical = best_moves[0]["move_pct"] if best_moves else 0
        if best_theoretical > 0:
            self.avg_efficiency = min(our_recent_pnl / (best_theoretical + 0.001) * 100, 100)

        # Refine strategy: adjust indicator weights based on win rate
        await self._refine_strategy()

    async def _refine_strategy(self):
        """Adjust strategy weights based on which indicators are performing best."""
        if not self.indicator_performance:
            return

        adjustments = {}
        for indicator, perf in self.indicator_performance.items():
            if perf["trade_count"] < 3:
                continue
            win_rate = perf["wins"] / perf["trade_count"] if perf["trade_count"] > 0 else 0
            avg_pnl = perf["total_pnl"] / perf["trade_count"]

            # Boost indicators with >50% win rate, reduce others
            if win_rate > 0.6:
                adjustments[indicator] = 0.05  # increase weight
            elif win_rate < 0.3:
                adjustments[indicator] = -0.05  # decrease weight
            else:
                adjustments[indicator] = 0.0

        if adjustments:
            self.weight_adjustments = adjustments
            # Also adjust threshold: lower if we're missing good trades, raise if losing too much
            total_trades = sum(p["trade_count"] for p in self.indicator_performance.values())
            total_wins = sum(p["wins"] for p in self.indicator_performance.values())
            overall_win_rate = total_wins / total_trades if total_trades > 0 else 0.5

            if overall_win_rate > 0.6 and len(self.missed_opportunities) > 3:
                self.threshold_adjustment = -0.02  # be more aggressive
            elif overall_win_rate < 0.35:
                self.threshold_adjustment = 0.03  # be more conservative

            await self.bus.publish(Event(
                type=EventType.STRATEGY_ADJUSTMENT,
                payload={
                    "weight_adjustments": adjustments,
                    "threshold_adjustment": self.threshold_adjustment,
                    "win_rate": round(overall_win_rate, 3),
                    "evaluations_run": self.evaluations_run,
                },
                source="evaluator",
            ))

    def get_summary(self) -> dict:
        return {
            "evaluations_run": self.evaluations_run,
            "avg_efficiency": round(self.avg_efficiency, 1),
            "indicator_performance": {
                ind: {
                    "win_rate": round(p["wins"] / p["trade_count"] * 100, 1) if p["trade_count"] > 0 else 0,
                    "avg_pnl": round(p["total_pnl"] / p["trade_count"], 4) if p["trade_count"] > 0 else 0,
                    "trades": p["trade_count"],
                }
                for ind, p in self.indicator_performance.items()
            },
            "top_coins": sorted(
                [
                    {"symbol": sym, "pnl": round(p["total_pnl"], 4), "trades": p["wins"] + p["losses"]}
                    for sym, p in self.coin_performance.items()
                ],
                key=lambda x: x["pnl"],
                reverse=True,
            )[:10],
            "missed_opportunities": self.missed_opportunities[:5],
            "weight_adjustments": self.weight_adjustments,
            "threshold_adjustment": self.threshold_adjustment,
        }
