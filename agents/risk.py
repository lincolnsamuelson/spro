import asyncio
from models import Event, EventType, Side, Portfolio
from event_bus import EventBus


class RiskManager:
    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("risk")
        self.risk_cfg = config["risk"]
        self.portfolio: Portfolio | None = None
        self.halted = False
        self.min_cash = self.risk_cfg.get("min_cash_reserve", 0.50)

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PORTFOLIO_UPDATE:
                self._update_portfolio(event.payload)
            elif event.type == EventType.TRADE_SIGNAL:
                await self._evaluate(event.payload)

    def _update_portfolio(self, payload: dict):
        if self.portfolio is None:
            self.portfolio = Portfolio(
                cash=payload["cash"],
                total_value=payload["total_value"],
                realized_pnl=payload["realized_pnl"],
                peak_value=payload.get("peak_value", payload["total_value"]),
            )
        else:
            self.portfolio.cash = payload["cash"]
            self.portfolio.total_value = payload["total_value"]
            self.portfolio.realized_pnl = payload["realized_pnl"]
            self.portfolio.peak_value = payload.get("peak_value", self.portfolio.peak_value)
        self.portfolio.positions = {
            k: True for k in payload.get("position_symbols", [])
        }

        # Check max drawdown
        if self.portfolio.peak_value > 0:
            drawdown = (self.portfolio.peak_value - self.portfolio.total_value) / self.portfolio.peak_value
            if drawdown >= self.risk_cfg["max_drawdown"]:
                self.halted = True

    async def _evaluate(self, payload: dict):
        if self.portfolio is None:
            return
        if self.halted:
            return

        symbol = payload["symbol"]
        direction = payload["direction"]
        price = payload["price"]
        confidence = payload["confidence"]

        # No duplicate positions
        if direction == Side.BUY.value and symbol in self.portfolio.positions:
            return

        # Can only sell if we hold
        if direction == Side.SELL.value and symbol not in self.portfolio.positions:
            return

        # Position sizing: deploy available cash aggressively
        stop_loss_pct = self.risk_cfg["default_stop_loss_pct"]
        take_profit_pct = self.risk_cfg["default_take_profit_pct"]

        if direction == Side.BUY.value:
            available_cash = self.portfolio.cash - self.min_cash
            if available_cash < 1.0:
                return

            # Size based on risk per trade, but also ensure we deploy capital
            risk_amount = self.portfolio.total_value * self.risk_cfg["max_risk_per_trade"]
            if stop_loss_pct > 0:
                position_value = risk_amount / stop_loss_pct
            else:
                position_value = available_cash

            # Use the smaller of risk-based size and available cash
            position_value = min(position_value, available_cash)
            if position_value < 1.0:
                return

            quantity = position_value / price
            stop_loss = price * (1 - stop_loss_pct)
            take_profit = price * (1 + take_profit_pct)
        else:
            quantity = 0
            stop_loss = 0
            take_profit = 0

        await self.bus.publish(Event(
            type=EventType.ORDER_REQUEST,
            payload={
                "symbol": symbol,
                "side": direction,
                "quantity": quantity,
                "price": price,
                "confidence": confidence,
                "stop_loss": round(stop_loss, 2),
                "take_profit": round(take_profit, 2),
            },
            source="risk",
        ))
