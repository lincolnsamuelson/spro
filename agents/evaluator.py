import asyncio
import json
import os
import time
from collections import defaultdict, deque
from models import Event, EventType, Side
from event_bus import EventBus


LEARNINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "learnings.json")


class Evaluator:
    """Learns from every trade. Persists lessons to disk so mistakes are never
    repeated across restarts. Tracks:
    - Coins that consistently lose → blacklist
    - Indicator+coin combos that fail → penalty scores
    - Exit reasons (liquidated/stop_loss/trailing_stop) → pattern detection
    - Optimal leverage per coin based on actual results
    - Time-of-day patterns
    Publishes strategy adjustments and coin blacklist to the strategy engine."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("evaluator")
        self.price_snapshots: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self.completed_trades: list[dict] = []
        self.recent_signals: dict[str, dict] = defaultdict(dict)
        self.evaluations_run: int = 0
        self.avg_efficiency: float = 0.0
        self.missed_opportunities: list[dict] = []
        self.weight_adjustments: dict[str, float] = {}
        self.threshold_adjustment: float = 0.0

        # === Persistent learnings ===
        self.learnings = self._load_learnings()

    def _default_learnings(self) -> dict:
        return {
            # Per-coin stats: {symbol: {wins, losses, total_pnl, liquidations, stop_losses, trailing_wins, best_leverage, consecutive_losses}}
            "coins": {},
            # Per-indicator stats: {indicator: {wins, losses, total_pnl, trade_count}}
            "indicators": {},
            # Indicator+coin combos: {"indicator:symbol": {wins, losses, total_pnl}}
            "combos": {},
            # Blacklisted coins (3+ consecutive losses or 2+ liquidations)
            "blacklist": [],
            # Coins on probation (1 more loss = blacklisted)
            "probation": [],
            # Learned optimal leverage per coin
            "leverage_overrides": {},
            # Exit reason tracking
            "exit_patterns": {
                "liquidated": 0,
                "stop_loss": 0,
                "trailing_stop": 0,
                "signal_sell": 0,
            },
            # Rules learned (human-readable log of what the system learned)
            "rules_learned": [],
        }

    def _load_learnings(self) -> dict:
        if os.path.exists(LEARNINGS_PATH):
            try:
                with open(LEARNINGS_PATH) as f:
                    data = json.load(f)
                # Merge with defaults to handle new fields
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
            elif event.type in (EventType.TECHNICAL_SIGNAL, EventType.SENTIMENT_SIGNAL,
                                EventType.MOMENTUM_SIGNAL):
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
        trade["active_signals"] = dict(self.recent_signals.get(symbol, {}))
        self.completed_trades.append(trade)

        pnl = trade.get("pnl")
        if pnl is None:
            return  # Buy order, no P&L yet

        reason = trade.get("reason", "signal_sell")
        leverage = trade.get("leverage", 1)
        is_win = pnl > 0

        # --- Update coin learnings ---
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

        # --- Update indicator learnings ---
        indicators = self.learnings["indicators"]
        for ind_name, sig in trade.get("active_signals", {}).items():
            if ind_name not in indicators:
                indicators[ind_name] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "trade_count": 0}
            ip = indicators[ind_name]
            ip["trade_count"] += 1
            ip["total_pnl"] += pnl
            if is_win:
                ip["wins"] += 1
            else:
                ip["losses"] += 1

            # --- Update indicator+coin combo ---
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

        # --- Track exit patterns ---
        if reason in self.learnings["exit_patterns"]:
            self.learnings["exit_patterns"][reason] += 1

        # --- Learn leverage ---
        if is_win and leverage > 0:
            if pnl > c.get("best_pnl_at_leverage", {}).get(str(leverage), 0):
                c["best_leverage"] = leverage

        # --- Blacklist / probation rules ---
        self._update_blacklist(symbol)

        # --- Save after every trade ---
        self._save_learnings()

    def _update_blacklist(self, symbol: str):
        c = self.learnings["coins"].get(symbol, {})
        blacklist = self.learnings["blacklist"]
        probation = self.learnings["probation"]
        rules = self.learnings["rules_learned"]

        # Rule: 3+ consecutive losses → blacklist
        if c.get("consecutive_losses", 0) >= 3 and symbol not in blacklist:
            blacklist.append(symbol)
            if symbol in probation:
                probation.remove(symbol)
            rule = f"BLACKLISTED {symbol}: {c['consecutive_losses']} consecutive losses (total P&L: ${c['total_pnl']:.2f})"
            rules.append(rule)
            return

        # Rule: 2+ liquidations → blacklist
        if c.get("liquidations", 0) >= 2 and symbol not in blacklist:
            blacklist.append(symbol)
            if symbol in probation:
                probation.remove(symbol)
            rule = f"BLACKLISTED {symbol}: {c['liquidations']} liquidations — too volatile for our leverage"
            rules.append(rule)
            return

        # Rule: total P&L deeply negative with 5+ trades → blacklist
        total_trades = c.get("wins", 0) + c.get("losses", 0)
        if total_trades >= 5 and c.get("total_pnl", 0) < -5.0 and symbol not in blacklist:
            blacklist.append(symbol)
            rule = f"BLACKLISTED {symbol}: P&L ${c['total_pnl']:.2f} over {total_trades} trades — consistently unprofitable"
            rules.append(rule)
            return

        # Rule: 2 consecutive losses → probation
        if c.get("consecutive_losses", 0) >= 2 and symbol not in probation and symbol not in blacklist:
            probation.append(symbol)
            rule = f"PROBATION {symbol}: {c['consecutive_losses']} consecutive losses — reducing exposure"
            rules.append(rule)

        # Rule: Win after being on probation → remove from probation
        if c.get("consecutive_losses", 0) == 0 and symbol in probation:
            probation.remove(symbol)
            rule = f"OFF PROBATION {symbol}: won a trade, restored to full trading"
            rules.append(rule)

        # Rule: Reduce leverage after liquidation
        if c.get("liquidations", 0) >= 1 and symbol not in blacklist:
            current_lev = self.learnings["leverage_overrides"].get(symbol)
            new_lev = max(3, (current_lev or self.config["trading"]["default_leverage"]) // 2)
            if current_lev != new_lev:
                self.learnings["leverage_overrides"][symbol] = new_lev
                rule = f"LEVERAGE CUT {symbol}: reduced to {new_lev}x after liquidation"
                rules.append(rule)

        # Keep rules list manageable
        if len(rules) > 100:
            self.learnings["rules_learned"] = rules[-50:]

    async def _periodic_eval(self):
        while True:
            await asyncio.sleep(90)
            await self._evaluate()

    async def _evaluate(self):
        self.evaluations_run += 1
        now = time.time()
        window = 300

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
                })

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

        await self._refine_strategy()
        self._save_learnings()

    async def _refine_strategy(self):
        indicators = self.learnings["indicators"]
        if not indicators:
            return

        adjustments = {}
        for indicator, perf in indicators.items():
            if perf["trade_count"] < 2:
                continue
            win_rate = perf["wins"] / perf["trade_count"]
            if win_rate > 0.55:
                adjustments[indicator] = 0.04
            elif win_rate < 0.30:
                adjustments[indicator] = -0.06  # Punish losers harder
            else:
                adjustments[indicator] = 0.0

        if adjustments:
            self.weight_adjustments = adjustments
            total_trades = sum(p["trade_count"] for p in indicators.values())
            total_wins = sum(p["wins"] for p in indicators.values())
            overall_win_rate = total_wins / total_trades if total_trades > 0 else 0.5

            if overall_win_rate > 0.55 and len(self.missed_opportunities) > 3:
                self.threshold_adjustment = -0.03
            elif overall_win_rate < 0.30:
                self.threshold_adjustment = 0.04
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
        """Return indicator:coin combos with negative track record."""
        bad = {}
        for combo_key, stats in self.learnings["combos"].items():
            total = stats["wins"] + stats["losses"]
            if total >= 2 and stats["total_pnl"] < 0:
                win_rate = stats["wins"] / total if total > 0 else 0
                if win_rate < 0.35:
                    bad[combo_key] = round(stats["total_pnl"], 4)
        return bad

    def get_summary(self) -> dict:
        indicators = self.learnings["indicators"]
        coins = self.learnings["coins"]
        return {
            "evaluations_run": self.evaluations_run,
            "avg_efficiency": round(self.avg_efficiency, 1),
            "indicator_performance": {
                ind: {
                    "win_rate": round(p["wins"] / p["trade_count"] * 100, 1) if p["trade_count"] > 0 else 0,
                    "avg_pnl": round(p["total_pnl"] / p["trade_count"], 4) if p["trade_count"] > 0 else 0,
                    "trades": p["trade_count"],
                }
                for ind, p in indicators.items()
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
