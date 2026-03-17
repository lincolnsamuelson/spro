from __future__ import annotations
import asyncio
import json
import os
import time
from models import Event, EventType, Side, Position, Portfolio
from event_bus import EventBus


class PortfolioManager:
    """Thread-safe shared portfolio state. All traders allocate/release capital
    through this manager, protected by asyncio.Lock."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("portfolio_manager", topics={
            EventType.SHUTDOWN,
        })
        self._lock = asyncio.Lock()
        self.risk_cfg = config["risk"]
        self.slippage = self.risk_cfg["simulated_slippage_pct"]
        self.trailing_stop_pct = self.risk_cfg["trailing_stop_pct"]
        self.portfolio = Portfolio(
            cash=config["trading"]["starting_balance"],
            total_value=config["trading"]["starting_balance"],
            peak_value=config["trading"]["starting_balance"],
        )
        self.latest_prices: dict[str, float] = {}
        self.trade_history: list[dict] = []
        self.position_owners: dict[str, str] = {}  # symbol -> trader_id
        self.halted = False
        self._load_state()

    def _state_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "portfolio.json")

    def _load_state(self):
        path = self._state_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                self.portfolio.cash = state["cash"]
                self.portfolio.realized_pnl = state["realized_pnl"]
                self.portfolio.peak_value = state["peak_value"]
                self.portfolio.win_streak = state.get("win_streak", 0)
                self.portfolio.total_wins = state.get("total_wins", 0)
                self.portfolio.total_losses = state.get("total_losses", 0)
                for sym, pos_data in state.get("positions", {}).items():
                    trader_id = pos_data.get("trader_id", "")
                    self.portfolio.positions[sym] = Position(
                        symbol=sym,
                        side=Side(pos_data["side"]),
                        entry_price=pos_data["entry_price"],
                        quantity=pos_data["quantity"],
                        leverage=pos_data.get("leverage", 1),
                        stop_loss=pos_data["stop_loss"],
                        trailing_stop=pos_data.get("trailing_stop", pos_data["stop_loss"]),
                        highest_price=pos_data.get("highest_price", pos_data["entry_price"]),
                        margin=pos_data.get("margin", 0),
                        trader_id=trader_id,
                    )
                    self.position_owners[sym] = trader_id
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_state(self):
        state = {
            "cash": round(self.portfolio.cash, 4),
            "realized_pnl": round(self.portfolio.realized_pnl, 4),
            "peak_value": round(self.portfolio.peak_value, 4),
            "win_streak": self.portfolio.win_streak,
            "total_wins": self.portfolio.total_wins,
            "total_losses": self.portfolio.total_losses,
            "positions": {},
        }
        for sym, pos in self.portfolio.positions.items():
            state["positions"][sym] = {
                "side": pos.side.value,
                "entry_price": pos.entry_price,
                "quantity": pos.quantity,
                "leverage": pos.leverage,
                "stop_loss": pos.stop_loss,
                "trailing_stop": pos.trailing_stop,
                "highest_price": pos.highest_price,
                "margin": pos.margin,
                "trader_id": pos.trader_id,
            }
        with open(self._state_path(), "w") as f:
            json.dump(state, f, indent=2)

    async def run(self):
        # Price update listener + trailing stop checker run as separate tasks
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
            self._drain(self.queue),
        )

    async def _drain(self, queue):
        while True:
            event = await queue.get()
            if event.type == EventType.SHUTDOWN:
                self._save_state()
                return

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
            if symbol not in self.portfolio.positions:
                return

            pos = self.portfolio.positions[symbol]

            if pos.side == Side.BUY:
                if price > pos.highest_price:
                    pos.highest_price = price
                    new_trailing = price * (1 - self.trailing_stop_pct)
                    if new_trailing > pos.trailing_stop:
                        pos.trailing_stop = new_trailing

                loss_pct = (pos.entry_price - price) / pos.entry_price
                leveraged_loss_pct = loss_pct * pos.leverage
                if leveraged_loss_pct >= 0.90:
                    await self._close_position(symbol, price, "liquidated")
                    return

                if price <= pos.trailing_stop and pos.trailing_stop > pos.stop_loss:
                    await self._close_position(symbol, price, "trailing_stop")
                elif price <= pos.stop_loss:
                    await self._close_position(symbol, price, "stop_loss")

            self._update_portfolio_value()

    async def try_allocate(self, trader_id: str, symbol: str, margin: float) -> bool:
        """Atomically check and reserve cash for a position."""
        async with self._lock:
            if symbol in self.portfolio.positions:
                return False
            if self.portfolio.cash - margin < 0.01:
                return False
            if self.halted:
                return False
            # Reserve cash
            self.portfolio.cash -= margin
            return True

    async def _execute_order(self, payload: dict):
        symbol = payload["symbol"]
        side = payload["side"]
        price = payload["price"]
        leverage = payload.get("leverage", self.config["trading"]["default_leverage"])
        trader_id = payload.get("trader_id", "")
        now = time.time()

        if side == Side.BUY.value:
            fill_price = price * (1 + self.slippage)
            margin = payload.get("margin", 0)

            # Try to allocate
            success = await self.try_allocate(trader_id, symbol, margin)
            if not success:
                return

            async with self._lock:
                notional = margin * leverage
                quantity = notional / fill_price
                stop_loss = payload.get("stop_loss", fill_price * (1 - self.risk_cfg["default_stop_loss_pct"]))
                trailing_stop = stop_loss

                self.portfolio.positions[symbol] = Position(
                    symbol=symbol,
                    side=Side.BUY,
                    entry_price=fill_price,
                    quantity=quantity,
                    leverage=leverage,
                    stop_loss=stop_loss,
                    trailing_stop=trailing_stop,
                    highest_price=fill_price,
                    margin=margin,
                    trader_id=trader_id,
                )
                self.position_owners[symbol] = trader_id

                trade = {
                    "time": now,
                    "symbol": symbol,
                    "side": "BUY",
                    "quantity": round(quantity, 8),
                    "price": round(fill_price, 6),
                    "margin": round(margin, 4),
                    "leverage": leverage,
                    "notional": round(notional, 2),
                    "confidence": payload.get("confidence", 0),
                    "trader_id": trader_id,
                }
                self.trade_history.append(trade)
                self._update_portfolio_value()
                self._save_state()

            await self.bus.publish(Event(
                type=EventType.ORDER_FILLED,
                payload=trade,
                source="portfolio_manager",
            ))
            await self._publish_portfolio()

        elif side == Side.SELL.value:
            async with self._lock:
                if symbol not in self.portfolio.positions:
                    return
                pos = self.portfolio.positions[symbol]
                fill_price = price * (1 - self.slippage)

                price_change_pct = (fill_price - pos.entry_price) / pos.entry_price
                leveraged_pnl = pos.margin * price_change_pct * pos.leverage
                returned_capital = pos.margin + leveraged_pnl

                self.portfolio.cash += max(returned_capital, 0)
                self.portfolio.realized_pnl += leveraged_pnl

                if leveraged_pnl > 0:
                    self.portfolio.win_streak += 1
                    self.portfolio.total_wins += 1
                else:
                    self.portfolio.win_streak = 0
                    self.portfolio.total_losses += 1

                del self.portfolio.positions[symbol]
                self.position_owners.pop(symbol, None)

                trade = {
                    "time": now,
                    "symbol": symbol,
                    "side": "SELL",
                    "quantity": round(pos.quantity, 8),
                    "price": round(fill_price, 6),
                    "margin_returned": round(max(returned_capital, 0), 4),
                    "pnl": round(leveraged_pnl, 4),
                    "leverage": pos.leverage,
                    "trader_id": pos.trader_id,
                }
                self.trade_history.append(trade)
                self._update_portfolio_value()
                self._save_state()

            await self.bus.publish(Event(
                type=EventType.ORDER_FILLED,
                payload=trade,
                source="portfolio_manager",
            ))
            await self._publish_portfolio()

    async def _close_position(self, symbol: str, price: float, reason: str):
        """Must be called while holding self._lock."""
        if symbol not in self.portfolio.positions:
            return
        pos = self.portfolio.positions[symbol]
        fill_price = price * (1 - self.slippage)

        price_change_pct = (fill_price - pos.entry_price) / pos.entry_price
        leveraged_pnl = pos.margin * price_change_pct * pos.leverage
        returned_capital = pos.margin + leveraged_pnl

        if reason == "liquidated":
            returned_capital = 0
            leveraged_pnl = -pos.margin

        self.portfolio.cash += max(returned_capital, 0)
        self.portfolio.realized_pnl += leveraged_pnl

        if leveraged_pnl > 0:
            self.portfolio.win_streak += 1
            self.portfolio.total_wins += 1
        else:
            self.portfolio.win_streak = 0
            self.portfolio.total_losses += 1

        del self.portfolio.positions[symbol]
        self.position_owners.pop(symbol, None)

        trade = {
            "time": time.time(),
            "symbol": symbol,
            "side": "SELL",
            "reason": reason,
            "quantity": round(pos.quantity, 8),
            "price": round(fill_price, 6),
            "margin_returned": round(max(returned_capital, 0), 4),
            "pnl": round(leveraged_pnl, 4),
            "leverage": pos.leverage,
            "trader_id": pos.trader_id,
        }
        self.trade_history.append(trade)
        self._update_portfolio_value()
        self._save_state()

        await self.bus.publish(Event(
            type=EventType.ORDER_FILLED,
            payload=trade,
            source="portfolio_manager",
        ))
        await self._publish_portfolio()

    def _update_portfolio_value(self):
        total = self.portfolio.cash
        for sym, pos in self.portfolio.positions.items():
            price = self.latest_prices.get(sym, pos.entry_price)
            price_change_pct = (price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
            pos.unrealized_pnl = pos.margin * price_change_pct * pos.leverage
            pos_value = pos.margin + pos.unrealized_pnl
            total += max(pos_value, 0)
        self.portfolio.total_value = round(total, 4)
        if total > self.portfolio.peak_value:
            self.portfolio.peak_value = total

        # Check drawdown halt
        if self.portfolio.peak_value > 0:
            drawdown = (self.portfolio.peak_value - self.portfolio.total_value) / self.portfolio.peak_value
            if drawdown >= self.risk_cfg["max_drawdown"]:
                self.halted = True

    async def _publish_portfolio(self):
        await self.bus.publish(Event(
            type=EventType.PORTFOLIO_UPDATE,
            payload={
                "cash": round(self.portfolio.cash, 4),
                "total_value": round(self.portfolio.total_value, 4),
                "realized_pnl": round(self.portfolio.realized_pnl, 4),
                "peak_value": round(self.portfolio.peak_value, 4),
                "position_symbols": list(self.portfolio.positions.keys()),
                "win_streak": self.portfolio.win_streak,
                "total_wins": self.portfolio.total_wins,
                "total_losses": self.portfolio.total_losses,
                "positions": {
                    sym: {
                        "side": pos.side.value,
                        "entry_price": pos.entry_price,
                        "quantity": pos.quantity,
                        "leverage": pos.leverage,
                        "unrealized_pnl": round(pos.unrealized_pnl, 4),
                        "margin": round(pos.margin, 4),
                        "stop_loss": pos.stop_loss,
                        "trailing_stop": round(pos.trailing_stop, 6),
                        "highest_price": pos.highest_price,
                        "trader_id": pos.trader_id,
                    }
                    for sym, pos in self.portfolio.positions.items()
                },
            },
            source="portfolio_manager",
        ))
