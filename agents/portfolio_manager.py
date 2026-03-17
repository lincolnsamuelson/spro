from __future__ import annotations
import asyncio
import json
import os
import time
from models import Event, EventType, Side, Position, Portfolio
from event_bus import EventBus


# Competitor identities
TRADER_COLORS = {
    "blitz":    "#3b82f6",   # blue
    "phantom":  "#8b5cf6",   # purple
    "maverick": "#f97316",   # orange
    "viper":    "#22c55e",   # green
    "ghost":    "#ec4899",   # pink
}

TRADER_NAMES = {
    "blitz":    "BLITZ",
    "phantom":  "PHANTOM",
    "maverick": "MAVERICK",
    "viper":    "VIPER",
    "ghost":    "GHOST",
}


class TraderState:
    """Per-trader portfolio state for the competition."""
    def __init__(self, trader_id: str, cash: float, style: str = ""):
        self.trader_id = trader_id
        self.name = TRADER_NAMES.get(trader_id, trader_id.upper())
        self.style = style
        self.cash = cash
        self.starting_cash = cash
        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.total_value = cash
        self.peak_value = cash
        self.win_streak = 0
        self.total_wins = 0
        self.total_losses = 0
        self.color = TRADER_COLORS.get(trader_id, "#64748b")
        self.generation = 1
        self.times_fired = 0
        # Rolling equity history for the chart (timestamp_ms, value)
        self.equity_history: list[tuple[float, float]] = []


class PortfolioManager:
    """Competition manager. Each trader has independent $50 cash and positions.
    Multiple traders CAN hold the same coin. Positions keyed by trader_id:symbol."""

    def __init__(self, bus: EventBus, config: dict, trader_configs: list[dict]):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("portfolio_manager", topics={
            EventType.SHUTDOWN,
        })
        self._lock = asyncio.Lock()
        self.risk_cfg = config["risk"]
        self.slippage = self.risk_cfg["simulated_slippage_pct"]
        self.trailing_stop_pct = self.risk_cfg["trailing_stop_pct"]
        self.latest_prices: dict[str, float] = {}
        self.trade_history: list[dict] = []
        self.halted = False

        # Per-trader states
        per_trader_capital = config["trading"]["starting_balance"]
        self.traders: dict[str, TraderState] = {}
        for tc in trader_configs:
            tid = tc["id"]
            self.traders[tid] = TraderState(tid, per_trader_capital, tc["style"])

        # Global portfolio view (sum of all traders)
        total_starting = per_trader_capital * len(trader_configs)
        self.portfolio = Portfolio(
            cash=total_starting,
            total_value=total_starting,
            peak_value=total_starting,
        )

        self._load_state()

    def _state_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "portfolio.json")

    def _load_state(self):
        path = self._state_path()
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                state = json.load(f)
            for tid, ts in state.get("traders", {}).items():
                if tid not in self.traders:
                    continue
                t = self.traders[tid]
                t.cash = ts["cash"]
                t.realized_pnl = ts.get("realized_pnl", 0)
                t.peak_value = ts.get("peak_value", t.starting_cash)
                t.win_streak = ts.get("win_streak", 0)
                t.total_wins = ts.get("total_wins", 0)
                t.total_losses = ts.get("total_losses", 0)
                t.generation = ts.get("generation", 1)
                t.times_fired = ts.get("times_fired", 0)
                if t.generation > 1:
                    base_name = TRADER_NAMES.get(tid, tid.upper())
                    t.name = f"{base_name} {PortfolioManager._roman(t.generation)}"
                for sym, pos_data in ts.get("positions", {}).items():
                    t.positions[sym] = Position(
                        symbol=sym,
                        side=Side(pos_data["side"]),
                        entry_price=pos_data["entry_price"],
                        quantity=pos_data["quantity"],
                        leverage=pos_data.get("leverage", 1),
                        stop_loss=pos_data["stop_loss"],
                        trailing_stop=pos_data.get("trailing_stop", pos_data["stop_loss"]),
                        highest_price=pos_data.get("highest_price", pos_data["entry_price"]),
                        margin=pos_data.get("margin", 0),
                        trader_id=tid,
                    )
            self._rebuild_global()
        except (json.JSONDecodeError, KeyError):
            pass

    def _save_state(self):
        state = {"traders": {}}
        for tid, t in self.traders.items():
            td = {
                "cash": round(t.cash, 4),
                "realized_pnl": round(t.realized_pnl, 4),
                "peak_value": round(t.peak_value, 4),
                "style": t.style,
                "win_streak": t.win_streak,
                "total_wins": t.total_wins,
                "total_losses": t.total_losses,
                "generation": t.generation,
                "times_fired": t.times_fired,
                "positions": {},
            }
            for sym, pos in t.positions.items():
                td["positions"][sym] = {
                    "side": pos.side.value,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "leverage": pos.leverage,
                    "stop_loss": pos.stop_loss,
                    "trailing_stop": pos.trailing_stop,
                    "highest_price": pos.highest_price,
                    "margin": pos.margin,
                    "trader_id": tid,
                }
            state["traders"][tid] = td
        with open(self._state_path(), "w") as f:
            json.dump(state, f, indent=2)

    def _rebuild_global(self):
        """Rebuild global portfolio from all trader states."""
        total_cash = 0
        total_value = 0
        all_positions = {}
        total_wins = 0
        total_losses = 0
        for tid, t in self.traders.items():
            total_cash += t.cash
            tv = t.cash
            for sym, pos in t.positions.items():
                price = self.latest_prices.get(sym, pos.entry_price)
                pcp = (price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
                pos.unrealized_pnl = pos.margin * pcp * pos.leverage
                pv = pos.margin + pos.unrealized_pnl
                tv += max(pv, 0)
                all_positions[f"{tid}:{sym}"] = pos
            t.total_value = round(tv, 4)
            if tv > t.peak_value:
                t.peak_value = tv
            total_value += tv
            total_wins += t.total_wins
            total_losses += t.total_losses
        self.portfolio.cash = round(total_cash, 4)
        self.portfolio.total_value = round(total_value, 4)
        self.portfolio.positions = all_positions
        self.portfolio.total_wins = total_wins
        self.portfolio.total_losses = total_losses
        if total_value > self.portfolio.peak_value:
            self.portfolio.peak_value = total_value

    async def run(self):
        price_queue = self.bus.subscribe("portfolio_price_listener", topics={
            EventType.PRICE_UPDATE, EventType.SHUTDOWN,
        })
        order_queue = self.bus.subscribe("portfolio_order_listener", topics={
            EventType.ORDER_REQUEST, EventType.SHUTDOWN,
        })
        await self._publish_portfolio()
        await asyncio.gather(
            self._price_loop(price_queue),
            self._order_loop(order_queue),
            self._equity_snapshot_loop(),
            self._drain(self.queue),
        )

    async def _drain(self, queue):
        while True:
            event = await queue.get()
            if event.type == EventType.SHUTDOWN:
                self._save_state()
                return

    async def _equity_snapshot_loop(self):
        """Record each trader's equity every 5 seconds for the chart.
        Also checks if any trader should be FIRED (lost 5%+ of starting value)."""
        while True:
            await asyncio.sleep(5)
            now_ms = time.time() * 1000
            fire_list = []
            async with self._lock:
                self._rebuild_global()
                for tid, t in self.traders.items():
                    t.equity_history.append((now_ms, round(t.total_value, 2)))
                    # Keep last 30 min (~360 points at 5s)
                    if len(t.equity_history) > 400:
                        t.equity_history = t.equity_history[-360:]
                    # Check for firing: lost 5% of starting value
                    fire_threshold = t.starting_cash * 0.95
                    has_positions = len(t.positions) > 0
                    if t.total_value < fire_threshold and has_positions:
                        fire_list.append(tid)
            # Fire outside the lock
            for tid in fire_list:
                await self._fire_trader(tid)

    async def _fire_trader(self, trader_id: str):
        """Fire an underperforming trader: close all positions, new agent
        inherits whatever cash remains. No fresh capital injection."""
        async with self._lock:
            t = self.traders.get(trader_id)
            if not t:
                return

            # Close all positions at current prices
            symbols_to_close = list(t.positions.keys())
            for sym in symbols_to_close:
                pos = t.positions[sym]
                price = self.latest_prices.get(sym, pos.entry_price)
                fill_price = price * (1 - self.slippage)
                pcp = (fill_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
                leveraged_pnl = pos.margin * pcp * pos.leverage
                returned = max(pos.margin + leveraged_pnl, 0)
                t.cash += returned
                del t.positions[sym]

            old_value = round(t.cash, 2)  # what's left after liquidation

            # Bump generation
            t.times_fired += 1
            t.generation = t.generation + 1
            gen_label = self._roman(t.generation)
            base_name = TRADER_NAMES.get(trader_id, trader_id.upper())
            t.name = f"{base_name} {gen_label}"

            # New agent inherits whatever cash remains — NO fresh capital
            # Just reset stats and peak so the 5% fire threshold is relative to current cash
            t.starting_cash = t.cash
            t.realized_pnl = 0.0
            t.total_value = t.cash
            t.peak_value = t.cash
            t.win_streak = 0
            t.total_wins = 0
            t.total_losses = 0

            self._rebuild_global()
            self._save_state()

            trade = {
                "time": time.time(), "symbol": "---", "side": "FIRED",
                "reason": "fired", "pnl": round(old_value - t.starting_cash, 2),
                "trader_id": trader_id, "generation": t.generation,
                "new_name": t.name,
            }
            self.trade_history.append(trade)

        # Publish firing event so the trader resets its brain
        await self.bus.publish(Event(
            type=EventType.TRADER_FIRED,
            payload={
                "trader_id": trader_id,
                "generation": t.generation,
                "new_name": t.name,
                "old_value": old_value,
                "reason": f"Lost {round((1 - old_value / t.starting_cash) * 100, 1)}% — FIRED",
            },
            source="portfolio_manager",
        ))
        await self._publish_portfolio()

    @staticmethod
    def _roman(n: int) -> str:
        vals = [(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]
        result = ''
        for val, numeral in vals:
            while n >= val:
                result += numeral
                n -= val
        return result

    async def _price_loop(self, queue):
        while True:
            event = await queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PRICE_UPDATE:
                await self._on_price(event.payload)

    async def _order_loop(self, queue):
        while True:
            event = await queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.ORDER_REQUEST:
                await self._execute_order(event.payload)

    async def _on_price(self, payload: dict):
        symbol = payload["symbol"]
        price = payload["price"]
        self.latest_prices[symbol] = price

        async with self._lock:
            for tid, t in self.traders.items():
                if symbol not in t.positions:
                    continue
                pos = t.positions[symbol]
                if pos.side != Side.BUY:
                    continue

                if price > pos.highest_price:
                    pos.highest_price = price
                    new_trailing = price * (1 - self.trailing_stop_pct)
                    if new_trailing > pos.trailing_stop:
                        pos.trailing_stop = new_trailing

                loss_pct = (pos.entry_price - price) / pos.entry_price
                leveraged_loss_pct = loss_pct * pos.leverage
                if leveraged_loss_pct >= 0.90:
                    await self._close_position(tid, symbol, price, "liquidated")
                    continue

                if price <= pos.trailing_stop and pos.trailing_stop > pos.stop_loss:
                    await self._close_position(tid, symbol, price, "trailing_stop")
                elif price <= pos.stop_loss:
                    await self._close_position(tid, symbol, price, "stop_loss")

            self._rebuild_global()

    async def _execute_order(self, payload: dict):
        trader_id = payload.get("trader_id", "")
        symbol = payload["symbol"]
        side = payload["side"]
        price = payload["price"]
        leverage = payload.get("leverage", self.config["trading"]["default_leverage"])
        now = time.time()

        if trader_id not in self.traders:
            return

        if side == Side.BUY.value:
            fill_price = price * (1 + self.slippage)
            margin = payload.get("margin", 0)

            async with self._lock:
                t = self.traders[trader_id]
                # Check this trader's cash
                if symbol in t.positions:
                    return
                if t.cash - margin < 0.01:
                    margin = t.cash - 0.01
                if margin < 0.10:
                    return

                t.cash -= margin
                notional = margin * leverage
                quantity = notional / fill_price
                stop_loss = payload.get("stop_loss", fill_price * (1 - self.risk_cfg["default_stop_loss_pct"]))

                t.positions[symbol] = Position(
                    symbol=symbol,
                    side=Side.BUY,
                    entry_price=fill_price,
                    quantity=quantity,
                    leverage=leverage,
                    stop_loss=stop_loss,
                    trailing_stop=stop_loss,
                    highest_price=fill_price,
                    margin=margin,
                    trader_id=trader_id,
                )

                trade = {
                    "time": now, "symbol": symbol, "side": "BUY",
                    "quantity": round(quantity, 8), "price": round(fill_price, 6),
                    "margin": round(margin, 4), "leverage": leverage,
                    "notional": round(notional, 2),
                    "confidence": payload.get("confidence", 0),
                    "trader_id": trader_id,
                }
                self.trade_history.append(trade)
                self._rebuild_global()
                self._save_state()

            await self.bus.publish(Event(
                type=EventType.ORDER_FILLED, payload=trade,
                source="portfolio_manager",
            ))
            await self._publish_portfolio()

        elif side == Side.SELL.value:
            async with self._lock:
                t = self.traders[trader_id]
                if symbol not in t.positions:
                    return
                pos = t.positions[symbol]
                fill_price = price * (1 - self.slippage)

                pcp = (fill_price - pos.entry_price) / pos.entry_price
                leveraged_pnl = pos.margin * pcp * pos.leverage
                returned_capital = pos.margin + leveraged_pnl

                t.cash += max(returned_capital, 0)
                t.realized_pnl += leveraged_pnl
                if leveraged_pnl > 0:
                    t.win_streak += 1
                    t.total_wins += 1
                else:
                    t.win_streak = 0
                    t.total_losses += 1

                del t.positions[symbol]

                trade = {
                    "time": now, "symbol": symbol, "side": "SELL",
                    "quantity": round(pos.quantity, 8), "price": round(fill_price, 6),
                    "margin_returned": round(max(returned_capital, 0), 4),
                    "pnl": round(leveraged_pnl, 4), "leverage": pos.leverage,
                    "trader_id": trader_id,
                }
                self.trade_history.append(trade)
                self._rebuild_global()
                self._save_state()

            await self.bus.publish(Event(
                type=EventType.ORDER_FILLED, payload=trade,
                source="portfolio_manager",
            ))
            await self._publish_portfolio()

    async def _close_position(self, trader_id: str, symbol: str, price: float, reason: str):
        """Must be called while holding self._lock."""
        t = self.traders.get(trader_id)
        if not t or symbol not in t.positions:
            return
        pos = t.positions[symbol]
        fill_price = price * (1 - self.slippage)

        pcp = (fill_price - pos.entry_price) / pos.entry_price
        leveraged_pnl = pos.margin * pcp * pos.leverage
        returned_capital = pos.margin + leveraged_pnl

        if reason == "liquidated":
            returned_capital = 0
            leveraged_pnl = -pos.margin

        t.cash += max(returned_capital, 0)
        t.realized_pnl += leveraged_pnl
        if leveraged_pnl > 0:
            t.win_streak += 1
            t.total_wins += 1
        else:
            t.win_streak = 0
            t.total_losses += 1

        del t.positions[symbol]

        trade = {
            "time": time.time(), "symbol": symbol, "side": "SELL",
            "reason": reason, "quantity": round(pos.quantity, 8),
            "price": round(fill_price, 6),
            "margin_returned": round(max(returned_capital, 0), 4),
            "pnl": round(leveraged_pnl, 4), "leverage": pos.leverage,
            "trader_id": trader_id,
        }
        self.trade_history.append(trade)
        self._rebuild_global()
        self._save_state()

        await self.bus.publish(Event(
            type=EventType.ORDER_FILLED, payload=trade,
            source="portfolio_manager",
        ))
        await self._publish_portfolio()

    def get_scoreboard(self) -> list[dict]:
        """Return sorted leaderboard for dashboard."""
        board = []
        for tid, t in self.traders.items():
            pnl_pct = ((t.total_value - t.starting_cash) / t.starting_cash * 100) if t.starting_cash > 0 else 0
            total_trades = t.total_wins + t.total_losses
            win_rate = (t.total_wins / total_trades * 100) if total_trades > 0 else 0
            board.append({
                "trader_id": tid,
                "name": t.name,
                "style": t.style,
                "color": t.color,
                "equity": round(t.total_value, 2),
                "cash": round(t.cash, 2),
                "pnl": round(t.total_value - t.starting_cash, 2),
                "pnl_pct": round(pnl_pct, 2),
                "realized_pnl": round(t.realized_pnl, 2),
                "positions": len(t.positions),
                "wins": t.total_wins,
                "losses": t.total_losses,
                "win_rate": round(win_rate, 1),
                "win_streak": t.win_streak,
                "generation": t.generation,
                "times_fired": t.times_fired,
            })
        board.sort(key=lambda x: x["equity"], reverse=True)
        for i, b in enumerate(board):
            b["rank"] = i + 1
        return board

    def get_equity_chart(self) -> dict:
        """Return per-trader equity history for the competition chart."""
        datasets = []
        for tid, t in self.traders.items():
            if not t.equity_history:
                continue
            datasets.append({
                "trader_id": tid,
                "name": t.name,
                "style": t.style,
                "color": t.color,
                "points": [{"x": ts, "y": val} for ts, val in t.equity_history],
            })
        return {"datasets": datasets, "starting_cash": t.starting_cash if self.traders else 50}

    async def _publish_portfolio(self):
        # Build combined position list with trader info
        positions_payload = {}
        for tid, t in self.traders.items():
            for sym, pos in t.positions.items():
                key = f"{tid}:{sym}"
                positions_payload[key] = {
                    "side": pos.side.value,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "leverage": pos.leverage,
                    "unrealized_pnl": round(pos.unrealized_pnl, 4),
                    "margin": round(pos.margin, 4),
                    "stop_loss": pos.stop_loss,
                    "trailing_stop": round(pos.trailing_stop, 6),
                    "highest_price": pos.highest_price,
                    "trader_id": tid,
                }

        all_position_symbols = []
        for t in self.traders.values():
            all_position_symbols.extend(t.positions.keys())

        await self.bus.publish(Event(
            type=EventType.PORTFOLIO_UPDATE,
            payload={
                "cash": round(self.portfolio.cash, 4),
                "total_value": round(self.portfolio.total_value, 4),
                "realized_pnl": round(sum(t.realized_pnl for t in self.traders.values()), 4),
                "peak_value": round(self.portfolio.peak_value, 4),
                "position_symbols": all_position_symbols,
                "win_streak": 0,
                "total_wins": self.portfolio.total_wins,
                "total_losses": self.portfolio.total_losses,
                "positions": positions_payload,
                # Per-trader cash for individual traders to read
                "trader_cash": {tid: round(t.cash, 4) for tid, t in self.traders.items()},
                "trader_positions": {tid: list(t.positions.keys()) for tid, t in self.traders.items()},
            },
            source="portfolio_manager",
        ))
