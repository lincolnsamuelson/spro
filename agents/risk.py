import asyncio
from models import Event, EventType, Side, Portfolio
from event_bus import EventBus


class RiskManager:
    """Aggressive risk manager. Max capital deployment, leverage enabled,
    no position limits. Only halts on catastrophic drawdown."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("risk")
        self.risk_cfg = config["risk"]
        self.portfolio: Portfolio | None = None
        self.halted = False
        self.min_cash = self.risk_cfg.get("min_cash_reserve", 0.10)

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

        # Only halt on catastrophic drawdown
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
        leverage = payload.get("leverage", self.config["trading"]["default_leverage"])
        multiplier = payload.get("multiplier", 1.0)

        # No duplicate positions
        if direction == Side.BUY.value and symbol in self.portfolio.positions:
            return

        # Can only sell if we hold
        if direction == Side.SELL.value and symbol not in self.portfolio.positions:
            return

        stop_loss_pct = self.risk_cfg["default_stop_loss_pct"]

        if direction == Side.BUY.value:
            available_cash = self.portfolio.cash - self.min_cash
            if available_cash < 0.50:
                return

            # Aggressive sizing: deploy large % of available cash per trade
            # Scale by confidence and compounding multiplier
            base_size = available_cash * self.risk_cfg["max_risk_per_trade"]
            position_margin = base_size * confidence * multiplier
            position_margin = min(position_margin, available_cash)
            if position_margin < 0.50:
                return

            # With leverage, notional value is margin * leverage
            notional_value = position_margin * leverage
            quantity = notional_value / price

            # Stop loss adjusted for leverage
            # With 10x leverage, a 2% move against = 20% of margin lost
            stop_loss = price * (1 - stop_loss_pct)

            await self.bus.publish(Event(
                type=EventType.ORDER_REQUEST,
                payload={
                    "symbol": symbol,
                    "side": direction,
                    "quantity": quantity,
                    "margin": round(position_margin, 4),
                    "price": price,
                    "confidence": confidence,
                    "leverage": leverage,
                    "stop_loss": round(stop_loss, 6),
                },
                source="risk",
            ))
        else:
            await self.bus.publish(Event(
                type=EventType.ORDER_REQUEST,
                payload={
                    "symbol": symbol,
                    "side": direction,
                    "quantity": 0,
                    "margin": 0,
                    "price": price,
                    "confidence": confidence,
                    "leverage": 1,
                    "stop_loss": 0,
                },
                source="risk",
            ))
