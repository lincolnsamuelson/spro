from __future__ import annotations
import asyncio
import time
from models import Event, EventType
from event_bus import EventBus


class RiskSentinel:
    """Real-time risk monitoring agent. Watches for:
    - Rapid drawdowns across traders (systemic risk)
    - Single trader meltdowns
    - Market-wide crashes (all coins dropping)
    - Concentration risk (too many traders in same coin)

    When danger detected, broadcasts RISK_ALERT to force defensive mode."""

    def __init__(self, bus: EventBus, config: dict, portfolio_mgr):
        self.bus = bus
        self.config = config
        self.portfolio_mgr = portfolio_mgr
        self.queue = bus.subscribe("risk_sentinel", topics={
            EventType.ORDER_FILLED, EventType.PRICE_UPDATE, EventType.SHUTDOWN,
        })
        self.risk_level: str = "normal"  # normal, elevated, critical
        self.alerts_sent: int = 0
        self.checks_run: int = 0
        self._last_total_value: float = 0.0
        self._last_check: float = 0.0
        self._value_snapshots: list[tuple[float, float]] = []  # (time, total_value)
        self._price_changes: dict[str, list[float]] = {}  # sym -> recent changes
        self.active_alerts: list[dict] = []

    async def run(self):
        monitor_task = asyncio.create_task(self._monitor_loop())
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                monitor_task.cancel()
                return
            if event.type == EventType.PRICE_UPDATE:
                self._track_price(event.payload)

    def _track_price(self, payload: dict):
        sym = payload["symbol"]
        price = payload["price"]
        if sym not in self._price_changes:
            self._price_changes[sym] = []
        self._price_changes[sym].append(price)
        if len(self._price_changes[sym]) > 100:
            self._price_changes[sym] = self._price_changes[sym][-60:]

    async def _monitor_loop(self):
        """Check risk every 5 seconds."""
        await asyncio.sleep(15)
        while True:
            await asyncio.sleep(5)
            await self._check_risk()

    async def _check_risk(self):
        self.checks_run += 1
        now = time.time()
        alerts = []

        # --- 1. SYSTEMIC DRAWDOWN CHECK ---
        total_value = self.portfolio_mgr.portfolio.total_value
        starting_total = sum(t.starting_cash for t in self.portfolio_mgr.traders.values())

        self._value_snapshots.append((now, total_value))
        if len(self._value_snapshots) > 120:
            self._value_snapshots = self._value_snapshots[-60:]

        # Check 30-second drawdown
        recent = [v for t, v in self._value_snapshots if now - t < 30]
        if len(recent) >= 2:
            max_recent = max(recent)
            if max_recent > 0:
                drawdown_30s = (max_recent - total_value) / max_recent
                if drawdown_30s > 0.05:  # 5% drawdown in 30 seconds
                    alerts.append({
                        "type": "rapid_drawdown",
                        "severity": "critical",
                        "message": f"Pool lost {drawdown_30s*100:.1f}% in 30s",
                        "action": "reduce_leverage",
                    })
                elif drawdown_30s > 0.02:
                    alerts.append({
                        "type": "drawdown_warning",
                        "severity": "elevated",
                        "message": f"Pool down {drawdown_30s*100:.1f}% in 30s",
                        "action": "tighten_stops",
                    })

        # --- 2. INDIVIDUAL TRADER MELTDOWN ---
        for tid, t in self.portfolio_mgr.traders.items():
            if t.starting_cash > 0:
                loss_pct = (t.starting_cash - t.total_value) / t.starting_cash
                if loss_pct > 0.30:  # 30% loss
                    alerts.append({
                        "type": "trader_meltdown",
                        "severity": "critical",
                        "trader_id": tid,
                        "message": f"{tid} down {loss_pct*100:.0f}%",
                        "action": "reduce_positions",
                    })

        # --- 3. MARKET-WIDE CRASH DETECTION ---
        dropping_coins = 0
        total_coins = 0
        for sym, prices in self._price_changes.items():
            if len(prices) < 5:
                continue
            total_coins += 1
            recent_change = (prices[-1] - prices[-5]) / prices[-5] if prices[-5] > 0 else 0
            if recent_change < -0.005:  # 0.5% drop
                dropping_coins += 1

        if total_coins > 0 and dropping_coins / total_coins > 0.7:
            alerts.append({
                "type": "market_crash",
                "severity": "critical",
                "message": f"{dropping_coins}/{total_coins} coins dropping",
                "action": "defensive_mode",
            })

        # --- 4. CONCENTRATION RISK ---
        coin_holders: dict[str, int] = {}
        for tid, t in self.portfolio_mgr.traders.items():
            for sym in t.positions:
                coin_holders[sym] = coin_holders.get(sym, 0) + 1

        for sym, count in coin_holders.items():
            if count >= 15:  # 75% of traders in same coin
                alerts.append({
                    "type": "concentration_risk",
                    "severity": "elevated",
                    "message": f"{count} traders hold {sym}",
                    "action": "diversify",
                })

        # Determine overall risk level
        if any(a["severity"] == "critical" for a in alerts):
            self.risk_level = "critical"
        elif any(a["severity"] == "elevated" for a in alerts):
            self.risk_level = "elevated"
        else:
            self.risk_level = "normal"

        self.active_alerts = alerts

        if alerts:
            self.alerts_sent += 1
            # Determine defensive action
            max_severity = "critical" if any(a["severity"] == "critical" for a in alerts) else "elevated"
            leverage_mult = 0.5 if max_severity == "critical" else 0.8
            threshold_mult = 1.5 if max_severity == "critical" else 1.2

            await self.bus.publish(Event(
                type=EventType.RISK_ALERT,
                payload={
                    "risk_level": self.risk_level,
                    "alerts": alerts,
                    "leverage_multiplier": leverage_mult,
                    "threshold_multiplier": threshold_mult,
                    "defensive_mode": max_severity == "critical",
                },
                source="risk_sentinel",
            ))

        self._last_total_value = total_value

    def get_summary(self) -> dict:
        return {
            "risk_level": self.risk_level,
            "checks_run": self.checks_run,
            "alerts_sent": self.alerts_sent,
            "active_alerts": self.active_alerts[-5:],
            "pool_value": round(self._last_total_value, 2),
        }
