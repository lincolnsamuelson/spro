from __future__ import annotations
import asyncio
import json
import os
import time
from collections import defaultdict, deque
from models import Event, EventType, Side, RESEARCHER_SIGNAL_TYPES
from event_bus import EventBus


LEARNINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "learnings.json")


class Evaluator:
    """Ruthless manager that learns from EVERY trade instantly.
    At 75x+ leverage, mistakes are fatal — react in seconds, not minutes.
    Blacklists fast, cuts leverage fast, adjusts strategy fast."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("evaluator", topics={
            EventType.PRICE_UPDATE, EventType.ORDER_FILLED,
            EventType.TECHNICAL_SIGNAL, EventType.SENTIMENT_SIGNAL,
            EventType.MOMENTUM_SIGNAL, EventType.ORDER_FLOW_SIGNAL,
            EventType.CORRELATION_SIGNAL, EventType.MICROSTRUCTURE_SIGNAL,
            EventType.SHUTDOWN,
        })
        self.price_snapshots: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self.completed_trades: list[dict] = []
        self.recent_signals: dict[str, dict] = defaultdict(dict)
        self.evaluations_run: int = 0
        self.avg_efficiency: float = 0.0
        self.missed_opportunities: list[dict] = []
        self.weight_adjustments: dict[str, float] = {}
        self.threshold_adjustment: float = 0.0
        self.learnings = self._load_learnings()
        self._last_broadcast = 0.0
        self._trades_since_broadcast = 0

    def _default_learnings(self) -> dict:
        return {
            "coins": {},
            "indicators": {},
            "combos": {},
            "traders": {},
            "blacklist": [],
            "probation": [],
            "leverage_overrides": {},
            "exit_patterns": {
                "liquidated": 0, "stop_loss": 0,
                "trailing_stop": 0, "signal_sell": 0,
            },
            "rules_learned": [],
        }

    def _load_learnings(self) -> dict:
        if os.path.exists(LEARNINGS_PATH):
            try:
                with open(LEARNINGS_PATH) as f:
                    data = json.load(f)
                defaults = self._default_learnings()
                for key in defaults:
                    if key not in data:
                        data[key] = defaults[key]
                return data
            except (json.JSONDecodeError, KeyError):
                pass
        return self._default_learnings()

    def _save_learnings(self):
        try:
            with open(LEARNINGS_PATH, "w") as f:
                json.dump(self.learnings, f, indent=2)
        except Exception:
            pass

    async def run(self):
        eval_task = asyncio.create_task(self._periodic_eval())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                eval_task.cancel()
                self._save_learnings()
                return
            if event.type == EventType.PRICE_UPDATE:
                self._record_price(event.payload)
            elif event.type == EventType.ORDER_FILLED:
                self._on_trade(event.payload)
            elif event.type in RESEARCHER_SIGNAL_TYPES:
                self._track_signal(event.payload)

    def _record_price(self, payload: dict):
        self.price_snapshots[payload["symbol"]].append(
            (payload["timestamp"], payload["price"]))

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
        trader_id = trade.get("trader_id", "")
        trade["active_signals"] = dict(self.recent_signals.get(symbol, {}))
        self.completed_trades.append(trade)
        # Keep memory bounded
        if len(self.completed_trades) > 500:
            self.completed_trades = self.completed_trades[-300:]

        pnl = trade.get("pnl")
        if pnl is None:
            return

        reason = trade.get("reason", "signal_sell")
        leverage = trade.get("leverage", 1)
        is_win = pnl > 0

        # --- Per-trader tracking ---
        if trader_id:
            traders = self.learnings["traders"]
            if trader_id not in traders:
                traders[trader_id] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
            tp = traders[trader_id]
            tp["total_pnl"] += pnl
            if is_win:
                tp["wins"] += 1
            else:
                tp["losses"] += 1

        # --- Per-coin tracking ---
        coins = self.learnings["coins"]
        if symbol not in coins:
            coins[symbol] = {
                "wins": 0, "losses": 0, "total_pnl": 0.0,
                "liquidations": 0, "stop_losses": 0, "trailing_wins": 0,
                "consecutive_losses": 0, "best_leverage": leverage,
                "worst_pnl": 0.0, "best_pnl": 0.0,
            }
        c = coins[symbol]
        c["total_pnl"] += pnl
        if is_win:
            c["wins"] += 1
            c["consecutive_losses"] = 0
            c["best_pnl"] = max(c["best_pnl"], pnl)
            if reason == "trailing_stop":
                c["trailing_wins"] += 1
        else:
            c["losses"] += 1
            c["consecutive_losses"] += 1
            c["worst_pnl"] = min(c["worst_pnl"], pnl)
            if reason == "liquidated":
                c["liquidations"] += 1
            elif reason == "stop_loss":
                c["stop_losses"] += 1

        # --- Per-indicator tracking ---
        indicators_data = self.learnings["indicators"]
        for ind_name, sig in trade.get("active_signals", {}).items():
            if ind_name not in indicators_data:
                indicators_data[ind_name] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "trade_count": 0}
            ip = indicators_data[ind_name]
            ip["trade_count"] += 1
            ip["total_pnl"] += pnl
            if is_win:
                ip["wins"] += 1
            else:
                ip["losses"] += 1

            combo_key = f"{ind_name}:{symbol}"
            combos = self.learnings["combos"]
            if combo_key not in combos:
                combos[combo_key] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
            cb = combos[combo_key]
            cb["total_pnl"] += pnl
            if is_win:
                cb["wins"] += 1
            else:
                cb["losses"] += 1

        if reason in self.learnings["exit_patterns"]:
            self.learnings["exit_patterns"][reason] += 1

        if is_win and leverage > 0:
            c["best_leverage"] = leverage

        # INSTANT reaction — blacklist/probation on every trade
        self._update_blacklist(symbol)
        self._save_learnings()

        # Broadcast adjustments after every few trades, not just on timer
        self._trades_since_broadcast += 1
        now = time.time()
        if self._trades_since_broadcast >= 3 or now - self._last_broadcast > 10:
            self._trades_since_broadcast = 0
            self._last_broadcast = now
            asyncio.create_task(self._broadcast_adjustments())

    def _update_blacklist(self, symbol: str):
        """AGGRESSIVE blacklisting. At 75x leverage, we can't afford patience."""
        c = self.learnings["coins"].get(symbol, {})
        blacklist = self.learnings["blacklist"]
        probation = self.learnings["probation"]
        rules = self.learnings["rules_learned"]

        # INSTANT BLACKLIST: 1 liquidation = banned
        if c.get("liquidations", 0) >= 1 and symbol not in blacklist:
            blacklist.append(symbol)
            if symbol in probation:
                probation.remove(symbol)
            rules.append(f"BLACKLISTED {symbol}: liquidated — zero tolerance at high leverage")
            return

        # 2 consecutive losses = blacklisted (was 3)
        if c.get("consecutive_losses", 0) >= 2 and symbol not in blacklist:
            blacklist.append(symbol)
            if symbol in probation:
                probation.remove(symbol)
            rules.append(f"BLACKLISTED {symbol}: {c['consecutive_losses']} consecutive losses")
            return

        # Negative P&L after just 3 trades = blacklisted (was 5)
        total_trades = c.get("wins", 0) + c.get("losses", 0)
        if total_trades >= 3 and c.get("total_pnl", 0) < -2.0 and symbol not in blacklist:
            blacklist.append(symbol)
            if symbol in probation:
                probation.remove(symbol)
            rules.append(f"BLACKLISTED {symbol}: P&L ${c['total_pnl']:.2f} over {total_trades} trades")
            return

        # 1 loss = probation (was 2)
        if c.get("consecutive_losses", 0) >= 1 and symbol not in probation and symbol not in blacklist:
            probation.append(symbol)
            rules.append(f"PROBATION {symbol}: lost a trade — watching closely")

        # Off probation only if 2 consecutive wins
        if c.get("consecutive_losses", 0) == 0 and c.get("wins", 0) >= 2 and symbol in probation:
            probation.remove(symbol)
            rules.append(f"OFF PROBATION {symbol}: proved itself with wins")

        # Any stop loss hit = cut leverage in half immediately
        if c.get("stop_losses", 0) >= 1 and symbol not in blacklist:
            current_lev = self.learnings["leverage_overrides"].get(symbol)
            default_lev = self.config["trading"]["default_leverage"]
            base = current_lev or default_lev
            new_lev = max(10, base // 2)
            if current_lev != new_lev:
                self.learnings["leverage_overrides"][symbol] = new_lev
                rules.append(f"LEVERAGE CUT {symbol}: {base}x -> {new_lev}x after stop loss")

        if len(rules) > 100:
            self.learnings["rules_learned"] = rules[-50:]

    async def _periodic_eval(self):
        """Evaluate every 15 seconds — fast feedback loop."""
        while True:
            await asyncio.sleep(15)
            await self._evaluate()

    async def _evaluate(self):
        self.evaluations_run += 1
        now = time.time()
        window = 120  # Look at last 2 minutes (was 5)

        best_moves = []
        for symbol, snapshots in self.price_snapshots.items():
            prices_in_window = [(t, p) for t, p in snapshots if now - t <= window]
            if len(prices_in_window) < 2:
                continue
            min_price = min(p for _, p in prices_in_window)
            max_price = max(p for _, p in prices_in_window)
            if min_price > 0:
                move_pct = (max_price - min_price) / min_price * 100
                best_moves.append({"symbol": symbol, "move_pct": round(move_pct, 3)})

        best_moves.sort(key=lambda x: x["move_pct"], reverse=True)
        traded_symbols = {t["symbol"] for t in self.completed_trades
                         if now - t.get("time", 0) <= window}
        self.missed_opportunities = [m for m in best_moves[:10]
                                     if m["symbol"] not in traded_symbols]

        our_recent_pnl = sum(
            t.get("pnl", 0) for t in self.completed_trades
            if t.get("pnl") is not None and now - t.get("time", 0) <= window
        )
        best_theoretical = best_moves[0]["move_pct"] if best_moves else 0
        if best_theoretical > 0:
            self.avg_efficiency = min(our_recent_pnl / (best_theoretical + 0.001) * 100, 100)

        await self._broadcast_adjustments()
        self._save_learnings()

    async def _broadcast_adjustments(self):
        """Send strategy adjustments to all traders."""
        indicators_data = self.learnings["indicators"]
        if not indicators_data:
            return

        adjustments = {}
        for indicator, perf in indicators_data.items():
            if perf["trade_count"] < 1:
                continue
            win_rate = perf["wins"] / perf["trade_count"]
            if win_rate > 0.55:
                adjustments[indicator] = 0.05
            elif win_rate < 0.35:
                adjustments[indicator] = -0.08  # More aggressive demotion
            else:
                adjustments[indicator] = 0.0

        self.weight_adjustments = adjustments
        total_trades = sum(p["trade_count"] for p in indicators_data.values())
        total_wins = sum(p["wins"] for p in indicators_data.values())
        overall_win_rate = total_wins / total_trades if total_trades > 0 else 0.5

        # If losing overall, raise thresholds (be pickier)
        # If winning, lower them (be more aggressive)
        if overall_win_rate > 0.55:
            self.threshold_adjustment = -0.03
        elif overall_win_rate < 0.40:
            self.threshold_adjustment = 0.05  # Much pickier when losing
        else:
            self.threshold_adjustment = 0.0

        await self.bus.publish(Event(
            type=EventType.STRATEGY_ADJUSTMENT,
            payload={
                "weight_adjustments": adjustments,
                "threshold_adjustment": self.threshold_adjustment,
                "win_rate": round(overall_win_rate, 3),
                "evaluations_run": self.evaluations_run,
                "blacklist": self.learnings["blacklist"],
                "probation": self.learnings["probation"],
                "leverage_overrides": self.learnings["leverage_overrides"],
                "bad_combos": self._get_bad_combos(),
            },
            source="evaluator",
        ))

    def _get_bad_combos(self) -> dict:
        bad = {}
        for combo_key, stats in self.learnings["combos"].items():
            total = stats["wins"] + stats["losses"]
            if total >= 1 and stats["total_pnl"] < 0:
                win_rate = stats["wins"] / total if total > 0 else 0
                if win_rate < 0.5:
                    bad[combo_key] = round(stats["total_pnl"], 4)
        return bad

    def get_summary(self) -> dict:
        indicators_data = self.learnings["indicators"]
        coins = self.learnings["coins"]
        traders = self.learnings.get("traders", {})
        return {
            "evaluations_run": self.evaluations_run,
            "avg_efficiency": round(self.avg_efficiency, 1),
            "indicator_performance": {
                ind: {
                    "win_rate": round(p["wins"] / p["trade_count"] * 100, 1) if p["trade_count"] > 0 else 0,
                    "avg_pnl": round(p["total_pnl"] / p["trade_count"], 4) if p["trade_count"] > 0 else 0,
                    "trades": p["trade_count"],
                }
                for ind, p in indicators_data.items()
            },
            "trader_performance": {
                tid: {
                    "win_rate": round(t["wins"] / (t["wins"] + t["losses"]) * 100, 1) if (t["wins"] + t["losses"]) > 0 else 0,
                    "pnl": round(t["total_pnl"], 4),
                    "trades": t["wins"] + t["losses"],
                }
                for tid, t in traders.items()
            },
            "top_coins": sorted(
                [{"symbol": sym, "pnl": round(p["total_pnl"], 4),
                  "trades": p["wins"] + p["losses"]}
                 for sym, p in coins.items()],
                key=lambda x: x["pnl"], reverse=True,
            )[:10],
            "missed_opportunities": self.missed_opportunities[:5],
            "weight_adjustments": self.weight_adjustments,
            "threshold_adjustment": self.threshold_adjustment,
            "blacklist": self.learnings["blacklist"],
            "probation": self.learnings["probation"],
            "rules_count": len(self.learnings["rules_learned"]),
            "recent_rules": self.learnings["rules_learned"][-5:],
            "exit_patterns": self.learnings["exit_patterns"],
        }
