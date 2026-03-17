import asyncio
import time
from models import Event, EventType
from event_bus import EventBus


class Compounder:
    """Monitors portfolio and ensures profits are immediately redeployed.
    Tracks win streaks and scales position sizes up during hot streaks.
    This agent ensures zero idle capital."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("compounder")
        self.comp_cfg = config["compounding"]
        self.scale_factor = self.comp_cfg["scale_factor"]
        self.win_streak_bonus = self.comp_cfg["win_streak_bonus"]
        self.reinvest_delay = self.comp_cfg["reinvest_delay_seconds"]
        # Track performance
        self.win_streak: int = 0
        self.loss_streak: int = 0
        self.current_multiplier: float = 1.0
        self.total_realized: float = 0.0
        self.cash_available: float = 0.0
        self.num_positions: int = 0
        self.last_compound_time: float = 0

    async def run(self):
        monitor_task = asyncio.create_task(self._monitor_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                monitor_task.cancel()
                return
            if event.type == EventType.ORDER_FILLED:
                self._on_trade(event.payload)
            elif event.type == EventType.PORTFOLIO_UPDATE:
                self._on_portfolio(event.payload)

    def _on_trade(self, payload: dict):
        pnl = payload.get("pnl")
        if pnl is not None:
            self.total_realized += pnl
            if pnl > 0:
                self.win_streak += 1
                self.loss_streak = 0
                # Scale up during win streaks
                self.current_multiplier = min(
                    self.scale_factor + (self.win_streak * self.win_streak_bonus),
                    3.0  # Cap at 3x
                )
            else:
                self.loss_streak += 1
                self.win_streak = 0
                # Scale down during loss streaks but don't go below 0.5x
                self.current_multiplier = max(
                    1.0 - (self.loss_streak * 0.15),
                    0.5
                )

    def _on_portfolio(self, payload: dict):
        self.cash_available = payload.get("cash", 0)
        self.num_positions = len(payload.get("position_symbols", []))

    async def _monitor_loop(self):
        """Continuously check if there's idle capital that should be deployed."""
        while True:
            await asyncio.sleep(self.reinvest_delay)
            now = time.time()

            # If ANY idle cash exists, signal to deploy it immediately
            if self.cash_available > 0.10 and now - self.last_compound_time > self.reinvest_delay:
                await self.bus.publish(Event(
                    type=EventType.COMPOUND_TRIGGER,
                    payload={
                        "idle_cash": round(self.cash_available, 2),
                        "multiplier": round(self.current_multiplier, 2),
                        "win_streak": self.win_streak,
                        "loss_streak": self.loss_streak,
                    },
                    source="compounder",
                ))
                self.last_compound_time = now

    def get_multiplier(self) -> float:
        return self.current_multiplier
