from __future__ import annotations
import asyncio
import time
from models import Event, EventType
from event_bus import EventBus


class SpeedCoach:
    """Monitors trading speed and ensures no trader is sitting idle.
    Tracks:
    - Time between trades per trader
    - Cash sitting idle (money not working)
    - Positions held too long without movement
    - Traders that aren't participating

    Broadcasts speed reports to wake up lazy traders."""

    def __init__(self, bus: EventBus, config: dict, portfolio_mgr):
        self.bus = bus
        self.config = config
        self.portfolio_mgr = portfolio_mgr
        self.queue = bus.subscribe("speed_coach", topics={
            EventType.ORDER_FILLED, EventType.SHUTDOWN,
        })
        self.last_trade_time: dict[str, float] = {}  # trader_id -> last trade time
        self.trade_counts: dict[str, int] = {}  # trader_id -> trade count
        self.checks_run: int = 0
        self.idle_traders: list[str] = []
        self.total_idle_cash: float = 0.0
        self.avg_trade_interval: float = 0.0
        self.speed_reports_sent: int = 0

    async def run(self):
        coach_task = asyncio.create_task(self._coach_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                coach_task.cancel()
                return
            if event.type == EventType.ORDER_FILLED:
                tid = event.payload.get("trader_id", "")
                if tid:
                    self.last_trade_time[tid] = time.time()
                    self.trade_counts[tid] = self.trade_counts.get(tid, 0) + 1

    async def _coach_loop(self):
        """Check trader speed every 10 seconds."""
        await asyncio.sleep(20)
        while True:
            await asyncio.sleep(10)
            await self._check_speed()

    async def _check_speed(self):
        self.checks_run += 1
        now = time.time()

        idle = []
        total_idle = 0.0
        intervals = []
        lazy_traders = []

        for tid, t in self.portfolio_mgr.traders.items():
            last = self.last_trade_time.get(tid, 0)
            trades = self.trade_counts.get(tid, 0)

            # Idle cash = wasted opportunity
            if t.cash > 1.0:
                total_idle += t.cash

            # Check if trader has been idle too long (>30s without a trade)
            idle_seconds = now - last if last > 0 else now - (now - 60)
            if idle_seconds > 30 and len(t.positions) < 5:
                idle.append(tid)
                lazy_traders.append({
                    "trader_id": tid,
                    "idle_seconds": round(idle_seconds, 0),
                    "idle_cash": round(t.cash, 2),
                    "positions": len(t.positions),
                    "total_trades": trades,
                })

            if last > 0:
                intervals.append(idle_seconds)

        self.idle_traders = idle
        self.total_idle_cash = total_idle
        self.avg_trade_interval = (sum(intervals) / len(intervals)) if intervals else 0

        # If there's significant idle cash or many idle traders, send alert
        if total_idle > 100 or len(idle) >= 5:
            self.speed_reports_sent += 1
            await self.bus.publish(Event(
                type=EventType.SPEED_REPORT,
                payload={
                    "idle_traders": lazy_traders[:10],
                    "total_idle_cash": round(total_idle, 2),
                    "idle_count": len(idle),
                    "avg_trade_interval": round(self.avg_trade_interval, 1),
                    "total_traders": len(self.portfolio_mgr.traders),
                },
                source="speed_coach",
            ))

    def get_summary(self) -> dict:
        return {
            "checks_run": self.checks_run,
            "idle_traders": len(self.idle_traders),
            "total_idle_cash": round(self.total_idle_cash, 2),
            "avg_trade_interval": round(self.avg_trade_interval, 1),
            "speed_reports_sent": self.speed_reports_sent,
            "trade_counts": dict(sorted(
                self.trade_counts.items(),
                key=lambda x: x[1], reverse=True
            )[:10]),
        }
