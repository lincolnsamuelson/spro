from __future__ import annotations
import asyncio
import time
from collections import defaultdict
from models import Event, EventType
from event_bus import EventBus


class StrategyOptimizer:
    """Continuously optimizes trading parameters based on live performance
    AND backtest results. Combines real trade outcomes with simulated tests
    to find the best rotation thresholds, leverage, and entry conditions.

    This is the brain that makes traders get better over time."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("strategy_optimizer", topics={
            EventType.ORDER_FILLED, EventType.BACKTEST_RESULT,
            EventType.MARKET_REGIME, EventType.SHUTDOWN,
        })
        # Live trade tracking
        self.trade_results: list[dict] = []
        self.per_config_results: dict[str, list] = defaultdict(list)

        # Current best parameters (starts with defaults)
        self.optimal_profit_target: float = 0.02
        self.optimal_loss_limit: float = -0.15
        self.optimal_leverage: int = 30
        self.optimal_threshold_mult: float = 1.0

        # Backtest recommendations
        self.backtest_best: dict = {}

        # Market regime adjustments
        self.regime_strategy: dict = {}
        self.current_regime: str = "ranging"

        # Stats
        self.optimizations_run: int = 0
        self.live_win_rate: float = 0.0
        self.live_avg_pnl: float = 0.0
        self._last_broadcast: float = 0.0

    async def run(self):
        optimize_task = asyncio.create_task(self._optimize_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                optimize_task.cancel()
                return
            if event.type == EventType.ORDER_FILLED:
                self._on_trade(event.payload)
            elif event.type == EventType.BACKTEST_RESULT:
                self._on_backtest(event.payload)
            elif event.type == EventType.MARKET_REGIME:
                self._on_regime(event.payload)

    def _on_trade(self, payload: dict):
        pnl = payload.get("pnl")
        if pnl is None:
            return  # BUY, not a completed trade
        self.trade_results.append({
            "pnl": pnl,
            "leverage": payload.get("leverage", 30),
            "symbol": payload.get("symbol", ""),
            "trader_id": payload.get("trader_id", ""),
            "time": time.time(),
        })
        # Bounded
        if len(self.trade_results) > 500:
            self.trade_results = self.trade_results[-300:]

    def _on_backtest(self, payload: dict):
        self.backtest_best = payload.get("best_config", {})

    def _on_regime(self, payload: dict):
        self.current_regime = payload.get("regime", "ranging")
        self.regime_strategy = payload.get("strategy", {})

    async def _optimize_loop(self):
        """Optimize every 20 seconds."""
        await asyncio.sleep(45)
        while True:
            await asyncio.sleep(20)
            await self._optimize()

    async def _optimize(self):
        self.optimizations_run += 1
        now = time.time()

        # Analyze recent live trades (last 2 minutes)
        recent = [t for t in self.trade_results if now - t["time"] < 120]
        if len(recent) < 3:
            # Not enough data — use backtest recommendations
            if self.backtest_best:
                self._apply_backtest()
            return

        wins = sum(1 for t in recent if t["pnl"] > 0)
        losses = sum(1 for t in recent if t["pnl"] <= 0)
        total = wins + losses
        self.live_win_rate = (wins / total * 100) if total > 0 else 0
        self.live_avg_pnl = sum(t["pnl"] for t in recent) / total if total > 0 else 0

        # --- ADAPTIVE OPTIMIZATION ---

        # If win rate is great (>60%), widen profit targets to capture more
        if self.live_win_rate > 60 and self.live_avg_pnl > 0:
            self.optimal_profit_target = min(0.05, self.optimal_profit_target * 1.1)
            self.optimal_loss_limit = max(-0.20, self.optimal_loss_limit * 1.05)

        # If win rate is good (40-60%), keep current
        elif self.live_win_rate >= 40:
            pass  # Don't change what's working

        # If win rate is bad (<40%), tighten profit (take what you can get)
        elif self.live_win_rate < 40:
            self.optimal_profit_target = max(0.01, self.optimal_profit_target * 0.9)
            # Also tighten loss limit to reduce bleeding
            self.optimal_loss_limit = min(-0.05, self.optimal_loss_limit * 0.9)

        # If we're losing money overall, get more conservative
        if self.live_avg_pnl < -0.5:
            self.optimal_leverage = max(20, self.optimal_leverage - 2)
            self.optimal_threshold_mult = min(1.5, self.optimal_threshold_mult + 0.05)

        # If we're making money, can be slightly more aggressive
        elif self.live_avg_pnl > 0.5:
            self.optimal_leverage = min(50, self.optimal_leverage + 1)
            self.optimal_threshold_mult = max(0.7, self.optimal_threshold_mult - 0.02)

        # Apply regime adjustments
        if self.regime_strategy:
            pt_mult = self.regime_strategy.get("profit_target_mult", 1.0)
            ll_mult = self.regime_strategy.get("loss_limit_mult", 1.0)
            lev_mult = self.regime_strategy.get("leverage_mult", 1.0)
            self.optimal_profit_target *= pt_mult
            self.optimal_loss_limit *= ll_mult
            self.optimal_leverage = int(self.optimal_leverage * lev_mult)

        # Clamp to sane ranges
        self.optimal_profit_target = max(0.005, min(0.10, self.optimal_profit_target))
        self.optimal_loss_limit = max(-0.25, min(-0.02, self.optimal_loss_limit))
        self.optimal_leverage = max(10, min(50, self.optimal_leverage))

        # Blend with backtest recommendations (70% live, 30% backtest)
        if self.backtest_best:
            bt_pt = self.backtest_best.get("profit_target", self.optimal_profit_target)
            bt_ll = self.backtest_best.get("loss_limit", self.optimal_loss_limit)
            bt_lev = self.backtest_best.get("leverage", self.optimal_leverage)
            self.optimal_profit_target = 0.7 * self.optimal_profit_target + 0.3 * bt_pt
            self.optimal_loss_limit = 0.7 * self.optimal_loss_limit + 0.3 * bt_ll
            self.optimal_leverage = int(0.7 * self.optimal_leverage + 0.3 * bt_lev)

        await self.bus.publish(Event(
            type=EventType.OPTIMIZER_UPDATE,
            payload={
                "profit_target": round(self.optimal_profit_target, 4),
                "loss_limit": round(self.optimal_loss_limit, 4),
                "leverage": self.optimal_leverage,
                "threshold_mult": round(self.optimal_threshold_mult, 3),
                "live_win_rate": round(self.live_win_rate, 1),
                "live_avg_pnl": round(self.live_avg_pnl, 4),
                "regime": self.current_regime,
                "optimizations_run": self.optimizations_run,
            },
            source="strategy_optimizer",
        ))
        self._last_broadcast = now

    def _apply_backtest(self):
        """When no live data, use backtest recommendations."""
        if not self.backtest_best:
            return
        self.optimal_profit_target = self.backtest_best.get("profit_target", 0.02)
        self.optimal_loss_limit = self.backtest_best.get("loss_limit", -0.15)
        self.optimal_leverage = self.backtest_best.get("leverage", 30)

    def get_summary(self) -> dict:
        return {
            "optimizations_run": self.optimizations_run,
            "live_win_rate": round(self.live_win_rate, 1),
            "live_avg_pnl": round(self.live_avg_pnl, 4),
            "optimal_profit_target": f"{self.optimal_profit_target * 100:.2f}%",
            "optimal_loss_limit": f"{self.optimal_loss_limit * 100:.2f}%",
            "optimal_leverage": self.optimal_leverage,
            "threshold_mult": round(self.optimal_threshold_mult, 3),
            "current_regime": self.current_regime,
            "backtest_best": self.backtest_best.get("label", "none"),
            "recent_trades": len(self.trade_results),
        }
