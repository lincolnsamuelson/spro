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
        prices = self.portfolio_mgr.latest_prices
        scoreboard = self.portfolio_mgr.get_scoreboard()

        lines = []
        w = 110

        lines.append("=" * w)
        lines.append(
            f"  TRADER COMPETITION  |  "
            f"5 Competitors x $50  |  "
            f"Total Pool: ${port.total_value:.2f}  |  "
            f"{len(prices)} coins  |  "
            f"PAPER MODE"
        )
        lines.append("=" * w)

        # === SCOREBOARD ===
        lines.append("")
        lines.append("  SCOREBOARD")
        lines.append(f"  {'Rank':<6}{'Trader':<20}{'Style':<20}{'Equity':>10}{'P&L':>10}{'P&L%':>8}{'Pos':>6}{'W/L':>10}{'WR%':>7}{'Streak':>8}")
        lines.append("  " + "-" * 105)
        for b in scoreboard:
            pnl_sign = "+" if b["pnl"] >= 0 else ""
            streak_str = f"x{b['win_streak']}" if b["win_streak"] > 0 else "---"
            medal = {1: "1st", 2: "2nd", 3: "3rd"}.get(b["rank"], f"{b['rank']}th")
            lines.append(
                f"  {medal:<6}"
                f"{b['trader_id']:<20}"
                f"{b['style']:<20}"
                f"${b['equity']:>8.2f}"
                f"  {pnl_sign}${abs(b['pnl']):>7.2f}"
                f"  {pnl_sign}{abs(b['pnl_pct']):>5.1f}%"
                f"  {b['positions']:>4}"
                f"  {b['wins']:>3}/{b['losses']:<3}"
                f"  {b['win_rate']:>5.1f}%"
                f"  {streak_str:>6}"
            )
        lines.append("")

        # === TRADER DETAIL ===
        lines.append("  TRADER STATUS")
        for t in self.traders:
            my_state = self.portfolio_mgr.traders.get(t.trader_id)
            cash = my_state.cash if my_state else 0
            pos_count = len(t.my_positions)
            pending = len(t.pending_symbols)
            lines.append(
                f"    {t.trader_id:25s}  "
                f"cash:${cash:>6.2f}  "
                f"pos:{pos_count}/{t.max_positions}  "
                f"pending:{pending}  "
                f"signals:{t.signals_received:>5d}  "
                f"trades:{t.trades_sent:>3d}"
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

        # Open positions (grouped by trader)
        lines.append("  OPEN POSITIONS")
        any_positions = False
        for tid, t_state in self.portfolio_mgr.traders.items():
            if not t_state.positions:
                continue
            any_positions = True
            for sym, pos in t_state.positions.items():
                equity = pos.margin + pos.unrealized_pnl
                pnl_pct_pos = (pos.unrealized_pnl / pos.margin * 100) if pos.margin > 0 else 0
                lines.append(
                    f"    [{tid[:12]:12s}] "
                    f"{sym[:14].upper():16s} "
                    f"{pos.leverage:2d}x  "
                    f"M:${pos.margin:>6.2f}  "
                    f"Eq:${equity:>6.2f}  "
                    f"P&L:${pos.unrealized_pnl:>+6.2f}({pnl_pct_pos:>+5.1f}%)"
                )
        if not any_positions:
            lines.append("    (no positions — deploying capital...)")
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
            f"Events:{summary['events_processed']}"
        )
        lines.append("=" * w)

        print("\n".join(lines))
