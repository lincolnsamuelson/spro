import asyncio
import json
import os
import time
from models import Event, EventType, Side, Position, Portfolio
from event_bus import EventBus


class Executor:
    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("executor")
        self.risk_cfg = config["risk"]
        self.slippage = self.risk_cfg["simulated_slippage_pct"]
        self.portfolio = Portfolio(
            cash=config["trading"]["starting_balance"],
            total_value=config["trading"]["starting_balance"],
            peak_value=config["trading"]["starting_balance"],
        )
        self.latest_prices: dict[str, float] = {}
        self.trade_history: list[dict] = []
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
                for sym, pos_data in state.get("positions", {}).items():
                    self.portfolio.positions[sym] = Position(
                        symbol=sym,
                        side=Side(pos_data["side"]),
                        entry_price=pos_data["entry_price"],
                        quantity=pos_data["quantity"],
                        stop_loss=pos_data["stop_loss"],
                        take_profit=pos_data["take_profit"],
                    )
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_state(self):
        state = {
            "cash": round(self.portfolio.cash, 2),
            "realized_pnl": round(self.portfolio.realized_pnl, 2),
            "peak_value": round(self.portfolio.peak_value, 2),
            "positions": {},
        }
        for sym, pos in self.portfolio.positions.items():
            state["positions"][sym] = {
                "side": pos.side.value,
                "entry_price": pos.entry_price,
                "quantity": pos.quantity,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
            }
        with open(self._state_path(), "w") as f:
            json.dump(state, f, indent=2)

    async def run(self):
        # Publish initial portfolio state
        await self._publish_portfolio()
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                self._save_state()
                return
            if event.type == EventType.PRICE_UPDATE:
                await self._on_price(event.payload)
            elif event.type == EventType.ORDER_REQUEST:
                await self._execute_order(event.payload)

    async def _on_price(self, payload: dict):
        symbol = payload["symbol"]
        price = payload["price"]
        self.latest_prices[symbol] = price

        # Check stop-loss and take-profit
        if symbol in self.portfolio.positions:
            pos = self.portfolio.positions[symbol]
            if pos.side == Side.BUY:
                if price <= pos.stop_loss:
                    await self._close_position(symbol, price, "stop_loss")
                elif price >= pos.take_profit:
                    await self._close_position(symbol, price, "take_profit")

        # Update unrealized P&L
        self._update_portfolio_value()

    async def _execute_order(self, payload: dict):
        symbol = payload["symbol"]
        side = payload["side"]
        price = payload["price"]
        now = time.time()

        if side == Side.BUY.value:
            # Apply slippage (buy at slightly higher price)
            fill_price = price * (1 + self.slippage)
            quantity = payload["quantity"]
            cost = fill_price * quantity

            if cost > self.portfolio.cash:
                quantity = (self.portfolio.cash * 0.99) / fill_price
                cost = fill_price * quantity

            if cost < 1.0:
                return

            self.portfolio.cash -= cost
            self.portfolio.positions[symbol] = Position(
                symbol=symbol,
                side=Side.BUY,
                entry_price=fill_price,
                quantity=quantity,
                stop_loss=payload["stop_loss"],
                take_profit=payload["take_profit"],
            )

            trade = {
                "time": now,
                "symbol": symbol,
                "side": "BUY",
                "quantity": round(quantity, 8),
                "price": round(fill_price, 2),
                "cost": round(cost, 2),
                "confidence": payload["confidence"],
            }

        elif side == Side.SELL.value:
            if symbol not in self.portfolio.positions:
                return
            pos = self.portfolio.positions[symbol]
            fill_price = price * (1 - self.slippage)
            proceeds = fill_price * pos.quantity
            pnl = (fill_price - pos.entry_price) * pos.quantity

            self.portfolio.cash += proceeds
            self.portfolio.realized_pnl += pnl
            del self.portfolio.positions[symbol]

            trade = {
                "time": now,
                "symbol": symbol,
                "side": "SELL",
                "quantity": round(pos.quantity, 8),
                "price": round(fill_price, 2),
                "proceeds": round(proceeds, 2),
                "pnl": round(pnl, 2),
            }
        else:
            return

        self.trade_history.append(trade)
        self._update_portfolio_value()
        self._save_state()

        await self.bus.publish(Event(
            type=EventType.ORDER_FILLED,
            payload=trade,
            source="executor",
        ))
        await self._publish_portfolio()

    async def _close_position(self, symbol: str, price: float, reason: str):
        if symbol not in self.portfolio.positions:
            return
        pos = self.portfolio.positions[symbol]
        fill_price = price * (1 - self.slippage)
        proceeds = fill_price * pos.quantity
        pnl = (fill_price - pos.entry_price) * pos.quantity

        self.portfolio.cash += proceeds
        self.portfolio.realized_pnl += pnl
        del self.portfolio.positions[symbol]

        trade = {
            "time": time.time(),
            "symbol": symbol,
            "side": "SELL",
            "reason": reason,
            "quantity": round(pos.quantity, 8),
            "price": round(fill_price, 2),
            "proceeds": round(proceeds, 2),
            "pnl": round(pnl, 2),
        }
        self.trade_history.append(trade)
        self._update_portfolio_value()
        self._save_state()

        await self.bus.publish(Event(
            type=EventType.ORDER_FILLED,
            payload=trade,
            source="executor",
        ))
        await self._publish_portfolio()

    def _update_portfolio_value(self):
        total = self.portfolio.cash
        for sym, pos in self.portfolio.positions.items():
            price = self.latest_prices.get(sym, pos.entry_price)
            pos.unrealized_pnl = (price - pos.entry_price) * pos.quantity
            total += price * pos.quantity
        self.portfolio.total_value = round(total, 2)
        if total > self.portfolio.peak_value:
            self.portfolio.peak_value = total

    async def _publish_portfolio(self):
        await self.bus.publish(Event(
            type=EventType.PORTFOLIO_UPDATE,
            payload={
                "cash": round(self.portfolio.cash, 2),
                "total_value": round(self.portfolio.total_value, 2),
                "realized_pnl": round(self.portfolio.realized_pnl, 2),
                "peak_value": round(self.portfolio.peak_value, 2),
                "position_symbols": list(self.portfolio.positions.keys()),
                "positions": {
                    sym: {
                        "side": pos.side.value,
                        "entry_price": pos.entry_price,
                        "quantity": pos.quantity,
                        "unrealized_pnl": round(pos.unrealized_pnl, 2),
                        "stop_loss": pos.stop_loss,
                        "take_profit": pos.take_profit,
                    }
                    for sym, pos in self.portfolio.positions.items()
                },
            },
            source="executor",
        ))
