import asyncio
import time
import os


class Dashboard:
    def __init__(self, executor, auditor, evaluator, compounder, volatility, config: dict):
        self.executor = executor
        self.auditor = auditor
        self.evaluator = evaluator
        self.compounder = compounder
        self.volatility = volatility
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
        port = self.executor.portfolio
        summary = self.auditor.get_summary()
        eval_summary = self.evaluator.get_summary()
        prices = self.executor.latest_prices
        starting = self.executor.config["trading"]["starting_balance"]
        pnl_pct = ((port.total_value - starting) / starting * 100) if starting > 0 else 0

        lines = []
        w = 95

        # Header
        lines.append("=" * w)
        lines.append(
            f"  AGGRESSIVE LEVERAGED TRADER   |   "
            f"Equity: ${port.total_value:.2f}   |   "
            f"Cash: ${port.cash:.2f}   |   "
            f"P&L: {pnl_pct:+.1f}%   |   "
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

        # Hot coins from volatility scanner
        hot = self.volatility.hot_coins[:8]
        if hot:
            hot_parts = []
            for sym in hot:
                score = self.volatility.volatility_scores.get(sym, 0)
                p = prices.get(sym, 0)
                if p > 1000:
                    hot_parts.append(f"{sym[:6].upper()}(v:{score:.1f})")
                else:
                    hot_parts.append(f"{sym[:6].upper()}(v:{score:.1f})")
            lines.append(f"  HOT COINS  {' | '.join(hot_parts)}")
        else:
            lines.append("  HOT COINS  (scanning...)")
        lines.append("-" * w)

        # Open positions with leverage and equity
        lines.append("  OPEN POSITIONS")
        total_margin = 0
        total_notional = 0
        total_unrealized = 0
        if port.positions:
            for sym, pos in port.positions.items():
                current = prices.get(sym, pos.entry_price)
                equity = pos.margin + pos.unrealized_pnl
                notional = pos.margin * pos.leverage
                total_margin += pos.margin
                total_notional += notional
                total_unrealized += pos.unrealized_pnl
                pnl_pct_pos = (pos.unrealized_pnl / pos.margin * 100) if pos.margin > 0 else 0
                lines.append(
                    f"    {sym[:14].upper():16s} "
                    f"{pos.leverage:2d}x  "
                    f"Margin:${pos.margin:>7.2f}  "
                    f"Notional:${notional:>9.2f}  "
                    f"Equity:${equity:>7.2f}  "
                    f"P&L:${pos.unrealized_pnl:>+7.2f}({pnl_pct_pos:>+5.1f}%)"
                )
                lines.append(
                    f"    {'':16s} "
                    f"     entry:${pos.entry_price:<10.4f} "
                    f"trail:${pos.trailing_stop:<10.4f} "
                    f"high:${pos.highest_price:<10.4f}"
                )
            lines.append(f"    {'─' * 80}")
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
        if eval_summary["indicator_performance"]:
            parts = []
            for ind, perf in eval_summary["indicator_performance"].items():
                parts.append(f"{ind[:5]}:{perf['win_rate']:.0f}%/{perf['trades']}t")
            lines.append(f"    {' | '.join(parts)}")
        if eval_summary["missed_opportunities"]:
            missed = eval_summary["missed_opportunities"][:3]
            missed_parts = [f"{m['symbol'][:8].upper()}({m['move_pct']:.1f}%)" for m in missed]
            lines.append(f"    Missed: {', '.join(missed_parts)}")
        if eval_summary["top_coins"]:
            top = eval_summary["top_coins"][:5]
            top_parts = [f"{c['symbol'][:8].upper()}:${c['pnl']:+.2f}" for c in top]
            lines.append(f"    Best coins: {' | '.join(top_parts)}")
        lines.append("-" * w)

        # Active signals (compact)
        lines.append("  SIGNALS")
        signal_items = list(summary.get("recent_signals", {}).items())
        shown = 0
        for sym, indicators in signal_items:
            if not indicators:
                continue
            parts = []
            for ind, data in indicators.items():
                d = "B" if data.get("direction") == "buy" else "S"
                s = data.get("strength", 0)
                parts.append(f"{ind[:4]}:{d}{s:.1f}")
            lines.append(f"    {sym[:12].upper():14s} {' | '.join(parts)}")
            shown += 1
            if shown >= 6:
                remaining = len(signal_items) - shown
                if remaining > 0:
                    lines.append(f"    ... {remaining} more")
                break
        if shown == 0:
            lines.append("    (warming up...)")
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
            margin_str = f"  margin:${margin:.2f}" if margin else ""
            reason = t.get("reason", "")
            reason_str = f" [{reason}]" if reason else ""
            lines.append(
                f"    {ts}  {side:4s} {sym:12s} {lev:2d}x{margin_str}{pnl_str}{reason_str}"
            )
        if not trades:
            lines.append("    (no trades yet)")
        lines.append("-" * w)

        # Stats
        lines.append(
            f"  Trades:{summary['total_trades']}  "
            f"Win:{summary['win_rate']}%  "
            f"P&L:${summary['total_pnl']:.2f}  "
            f"MaxDD:-{summary['max_drawdown']}%  "
            f"Events:{summary['events_processed']}"
        )
        lines.append("=" * w)

        print("\n".join(lines))
