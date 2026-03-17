import asyncio
import time
import os


class Dashboard:
    def __init__(self, portfolio_mgr, auditor, evaluator, compounder,
                 volatility, traders, config: dict):
        self.portfolio_mgr = portfolio_mgr
        self.auditor = auditor
        self.evaluator = evaluator
        self.compounder = compounder
        self.volatility = volatility
        self.traders = traders  # list of TraderAgent instances
        self.refresh = config["dashboard"]["refresh_interval_seconds"]

    async def run(self):
        while True:
            try:
                self._render()
            except Exception:
                pass
            await asyncio.sleep(self.refresh)

    def _render(self):
        os.system("clear" if os.name != "nt" else "cls")
        port = self.portfolio_mgr.portfolio
        summary = self.auditor.get_summary()
        eval_summary = self.evaluator.get_summary()
        prices = self.portfolio_mgr.latest_prices
        starting = self.portfolio_mgr.config["trading"]["starting_balance"]
        pnl_pct = ((port.total_value - starting) / starting * 100) if starting > 0 else 0

        lines = []
        w = 100

        lines.append("=" * w)
        lines.append(
            f"  SWARM TRADER  |  "
            f"10 Researchers -> 5 Traders  |  "
            f"Equity: ${port.total_value:.2f}  |  "
            f"Cash: ${port.cash:.2f}  |  "
            f"P&L: {pnl_pct:+.1f}%  |  "
            f"Realized: ${port.realized_pnl:+.2f}"
        )
        win_rate = (port.total_wins / (port.total_wins + port.total_losses) * 100
                    if (port.total_wins + port.total_losses) > 0 else 0)
        streak_str = f"Win x{port.win_streak}" if port.win_streak > 0 else "---"
        mult = self.compounder.get_multiplier()
        lines.append(
            f"  {len(prices)} coins | {len(port.positions)} positions | "
            f"W/L: {port.total_wins}/{port.total_losses} ({win_rate:.0f}%) | "
            f"Streak: {streak_str} | Multiplier: {mult:.1f}x | "
            f"PAPER MODE"
        )
        lines.append("=" * w)

        # Trader agents status
        lines.append("  TRADER AGENTS")
        for t in self.traders:
            pos_count = len(t.my_positions)
            pending = len(t.pending_symbols)
            lines.append(
                f"    {t.trader_id:25s}  style:{t.style:18s}  "
                f"pos:{pos_count}/{t.max_positions}  "
                f"pending:{pending}  "
                f"signals:{t.signals_received:>5d}  "
                f"trades:{t.trades_sent:>3d}  "
                f"cooldown:{t.cooldown}s  "
                f"thresh:{t.threshold:.2f}"
            )
        lines.append("-" * w)

        # Hot coins
        hot = self.volatility.hot_coins[:8]
        if hot:
            hot_parts = []
            for sym in hot:
                score = self.volatility.volatility_scores.get(sym, 0)
                hot_parts.append(f"{sym[:6].upper()}(v:{score:.1f})")
            lines.append(f"  HOT COINS  {' | '.join(hot_parts)}")
        else:
            lines.append("  HOT COINS  (scanning...)")
        lines.append("-" * w)

        # Open positions
        lines.append("  OPEN POSITIONS")
        total_margin = 0
        total_notional = 0
        total_unrealized = 0
        if port.positions:
            for sym, pos in port.positions.items():
                equity = pos.margin + pos.unrealized_pnl
                notional = pos.margin * pos.leverage
                total_margin += pos.margin
                total_notional += notional
                total_unrealized += pos.unrealized_pnl
                pnl_pct_pos = (pos.unrealized_pnl / pos.margin * 100) if pos.margin > 0 else 0
                owner = pos.trader_id[:12] if pos.trader_id else "?"
                lines.append(
                    f"    {sym[:14].upper():16s} "
                    f"{pos.leverage:2d}x  "
                    f"M:${pos.margin:>6.2f}  "
                    f"N:${notional:>8.2f}  "
                    f"Eq:${equity:>6.2f}  "
                    f"P&L:${pos.unrealized_pnl:>+6.2f}({pnl_pct_pos:>+5.1f}%)  "
                    f"[{owner}]"
                )
            lines.append(f"    {'─' * 85}")
            deployed_pct = (total_margin / port.total_value * 100) if port.total_value > 0 else 0
            lines.append(
                f"    TOTALS  Margin:${total_margin:>7.2f}  "
                f"Notional:${total_notional:>9.2f}  "
                f"Unrealized:${total_unrealized:>+7.2f}  "
                f"Deployed:{deployed_pct:.0f}%"
            )
        else:
            lines.append("    (no positions — deploying capital...)")
        lines.append("-" * w)

        # Evaluator
        lines.append("  EVALUATOR")
        lines.append(
            f"    Evals:{eval_summary['evaluations_run']}  "
            f"Efficiency:{eval_summary['avg_efficiency']}%  "
            f"Thresh adj:{eval_summary['threshold_adjustment']:+.3f}"
        )
        # Trader performance
        trader_perf = eval_summary.get("trader_performance", {})
        if trader_perf:
            tp_parts = []
            for tid, tp in trader_perf.items():
                tp_parts.append(f"{tid[:10]}:{tp['win_rate']:.0f}%/${tp['pnl']:+.2f}")
            lines.append(f"    Traders: {' | '.join(tp_parts)}")
        if eval_summary["indicator_performance"]:
            parts = []
            for ind, perf in eval_summary["indicator_performance"].items():
                parts.append(f"{ind[:5]}:{perf['win_rate']:.0f}%/{perf['trades']}t")
            lines.append(f"    {' | '.join(parts)}")
        blacklist = eval_summary.get("blacklist", [])
        probation = eval_summary.get("probation", [])
        if blacklist:
            lines.append(f"    BLACKLISTED: {', '.join(s[:8].upper() for s in blacklist[:8])}")
        if probation:
            lines.append(f"    PROBATION:   {', '.join(s[:8].upper() for s in probation[:8])}")
        recent_rules = eval_summary.get("recent_rules", [])
        if recent_rules:
            lines.append(f"    Last rule: {recent_rules[-1][:80]}")
        lines.append("-" * w)

        # Recent trades
        lines.append("  RECENT TRADES")
        trades = summary.get("recent_trades", [])
        for t in trades[-8:]:
            ts = time.strftime("%H:%M:%S", time.localtime(t.get("time", 0)))
            sym = t.get("symbol", "?")[:10].upper()
            side = t.get("side", "?")
            lev = t.get("leverage", 1)
            pnl = t.get("pnl")
            pnl_str = f"  P&L:${pnl:+.2f}" if pnl is not None else ""
            margin = t.get("margin") or t.get("margin_returned")
            margin_str = f"  m:${margin:.2f}" if margin else ""
            reason = t.get("reason", "")
            reason_str = f" [{reason}]" if reason else ""
            trader = t.get("trader_id", "")[:10]
            lines.append(
                f"    {ts}  {side:4s} {sym:12s} {lev:2d}x{margin_str}{pnl_str}{reason_str}  [{trader}]"
            )
        if not trades:
            lines.append("    (no trades yet)")
        lines.append("-" * w)

        lines.append(
            f"  Trades:{summary['total_trades']}  "
            f"Win:{summary['win_rate']}%  "
            f"P&L:${summary['total_pnl']:.2f}  "
            f"MaxDD:-{summary['max_drawdown']}%  "
            f"Events:{summary['events_processed']}"
        )
        lines.append("=" * w)

        print("\n".join(lines))
