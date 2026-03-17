import asyncio
import time
import os
from models import Side


class Dashboard:
    def __init__(self, executor, auditor, evaluator, config: dict):
        self.executor = executor
        self.auditor = auditor
        self.evaluator = evaluator
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
        w = 90
        lines.append("=" * w)
        lines.append(f"  CRYPTO PAPER TRADER   |   "
                     f"Total Equity: ${port.total_value:.2f}   |   "
                     f"Cash: ${port.cash:.2f}   |   "
                     f"P&L: {pnl_pct:+.2f}%")
        lines.append(f"  Tracking {len(prices)} coins   |   "
                     f"{len(port.positions)} open positions   |   "
                     f"Mode: PAPER   |   Ctrl+C to stop")
        lines.append("=" * w)

        # Prices - compact grid
        lines.append("  PRICES (top 20)")
        price_items = list(prices.items())[:20]
        row = "    "
        for i, (sym, price) in enumerate(price_items):
            label = sym[:5].upper()
            if price > 1000:
                entry = f"{label}:${price:,.0f}"
            elif price > 1:
                entry = f"{label}:${price:.2f}"
            else:
                entry = f"{label}:${price:.5f}"
            row += f"{entry:18s}"
            if (i + 1) % 5 == 0:
                lines.append(row)
                row = "    "
        if row.strip():
            lines.append(row)
        if len(prices) > 20:
            lines.append(f"    ... and {len(prices) - 20} more coins tracked")
        lines.append("-" * w)

        # Open positions with equity breakdown
        lines.append("  OPEN POSITIONS & EQUITY")
        total_invested = 0
        if port.positions:
            for sym, pos in port.positions.items():
                current = prices.get(sym, pos.entry_price)
                equity = current * pos.quantity
                cost_basis = pos.entry_price * pos.quantity
                pnl = equity - cost_basis
                pnl_pct_pos = ((current - pos.entry_price) / pos.entry_price * 100) if pos.entry_price else 0
                total_invested += equity
                lines.append(
                    f"    {sym[:14].upper():16s} "
                    f"Equity:${equity:>8.2f}  "
                    f"Cost:${cost_basis:>8.2f}  "
                    f"P&L:${pnl:>+7.2f} ({pnl_pct_pos:>+5.1f}%)  "
                    f"qty:{pos.quantity:.6f}"
                )
            lines.append(f"    {'':16s} {'─' * 50}")
            lines.append(
                f"    {'TOTAL INVESTED':16s} "
                f"Equity:${total_invested:>8.2f}  "
                f"Cash:${port.cash:>8.2f}  "
                f"= ${port.total_value:>8.2f} total"
            )
            deployed_pct = (total_invested / port.total_value * 100) if port.total_value > 0 else 0
            lines.append(f"    Capital deployed: {deployed_pct:.1f}%")
        else:
            lines.append("    (no open positions — capital idle)")
        lines.append("-" * w)

        # Evaluator intelligence
        lines.append("  EVALUATOR")
        lines.append(f"    Evaluations: {eval_summary['evaluations_run']}  |  "
                     f"Efficiency: {eval_summary['avg_efficiency']}%  |  "
                     f"Thresh adj: {eval_summary['threshold_adjustment']:+.3f}")
        if eval_summary["indicator_performance"]:
            parts = []
            for ind, perf in eval_summary["indicator_performance"].items():
                parts.append(f"{ind}:{perf['win_rate']}%w/{perf['trades']}t")
            lines.append(f"    Indicators: {' | '.join(parts)}")
        if eval_summary["weight_adjustments"]:
            adj_parts = [f"{k}:{v:+.2f}" for k, v in eval_summary["weight_adjustments"].items() if v != 0]
            if adj_parts:
                lines.append(f"    Weight tweaks: {' | '.join(adj_parts)}")
        if eval_summary["missed_opportunities"]:
            missed = eval_summary["missed_opportunities"][:3]
            missed_parts = [f"{m['symbol'][:8].upper()}({m['move_pct']:.2f}%)" for m in missed]
            lines.append(f"    Missed movers: {', '.join(missed_parts)}")
        lines.append("-" * w)

        # Active signals (compact)
        lines.append("  ACTIVE SIGNALS")
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
            if shown >= 8:
                remaining = len(signal_items) - shown
                if remaining > 0:
                    lines.append(f"    ... {remaining} more with signals")
                break
        if shown == 0:
            lines.append("    (warming up...)")
        lines.append("-" * w)

        # Recent trades
        lines.append("  RECENT TRADES")
        trades = summary.get("recent_trades", [])
        for t in trades[-6:]:
            ts = time.strftime("%H:%M:%S", time.localtime(t.get("time", 0)))
            sym = t.get("symbol", "?")[:10].upper()
            side = t.get("side", "?")
            qty = t.get("quantity", 0)
            price = t.get("price", 0)
            pnl = t.get("pnl")
            pnl_str = f"  P&L:${pnl:+.2f}" if pnl is not None else ""
            reason = t.get("reason", "")
            reason_str = f" [{reason}]" if reason else ""
            lines.append(
                f"    {ts}  {side:4s} {sym:12s} {qty:.6f} @ ${price:,.2f}{pnl_str}{reason_str}"
            )
        if not trades:
            lines.append("    (no trades yet)")
        lines.append("-" * w)

        # Stats
        lines.append(
            f"  STATS   Trades:{summary['total_trades']}  "
            f"Win:{summary['win_rate']}%  "
            f"TotalP&L:${summary['total_pnl']:.2f}  "
            f"AvgP&L:${summary['avg_pnl']:.2f}  "
            f"MaxDD:-{summary['max_drawdown']}%  "
            f"Events:{summary['events_processed']}"
        )
        lines.append("=" * w)

        print("\n".join(lines))
