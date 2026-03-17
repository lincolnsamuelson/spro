import asyncio
import time
from models import Event, EventType, Side, Portfolio
from event_bus import EventBus


class RiskManager:
    """Aggressive risk manager. Splits cash across multiple simultaneous trades.
    Tracks pending allocations so rapid-fire signals all get funded."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("risk")
        self.risk_cfg = config["risk"]
        self.portfolio: Portfolio | None = None
        self.halted = False
        self.min_cash = self.risk_cfg.get("min_cash_reserve", 0.01)
        # Track cash allocated to pending orders (not yet filled)
        self.pending_allocations: dict[str, float] = {}
        self.pending_expiry: dict[str, float] = {}

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.PORTFOLIO_UPDATE:
                self._update_portfolio(event.payload)
            elif event.type == EventType.TRADE_SIGNAL:
                await self._evaluate(event.payload)
            elif event.type == EventType.ORDER_FILLED:
                # Clear pending allocation when order fills
                symbol = event.payload.get("symbol", "")
                self.pending_allocations.pop(symbol, None)
                self.pending_expiry.pop(symbol, None)

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

        if self.portfolio.peak_value > 0:
            drawdown = (self.portfolio.peak_value - self.portfolio.total_value) / self.portfolio.peak_value
            if drawdown >= self.risk_cfg["max_drawdown"]:
                self.halted = True

    def _effective_cash(self) -> float:
        """Cash minus pending allocations that haven't expired."""
        now = time.time()
        # Expire stale pending allocations (>10s old = order probably failed)
        expired = [s for s, t in self.pending_expiry.items() if now - t > 10]
        for s in expired:
            self.pending_allocations.pop(s, None)
            self.pending_expiry.pop(s, None)

        pending_total = sum(self.pending_allocations.values())
        return self.portfolio.cash - pending_total - self.min_cash

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

        if direction == Side.BUY.value and symbol in self.portfolio.positions:
            return
        if direction == Side.SELL.value and symbol not in self.portfolio.positions:
            return
        # Don't double-allocate to same symbol
        if direction == Side.BUY.value and symbol in self.pending_allocations:
            return

        stop_loss_pct = self.risk_cfg["default_stop_loss_pct"]

        if direction == Side.BUY.value:
            available = self._effective_cash()
            if available < 0.10:
                return

            # Use all available cash — the strategy already picked the best coins
            position_margin = available
            if position_margin < 0.10:
                return

            notional_value = position_margin * leverage
            quantity = notional_value / price
            stop_loss = price * (1 - stop_loss_pct)

            # Reserve this cash so next signal sees reduced availability
            self.pending_allocations[symbol] = position_margin
            self.pending_expiry[symbol] = time.time()

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
