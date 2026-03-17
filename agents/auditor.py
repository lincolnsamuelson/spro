import asyncio
import json
import os
import time
from dataclasses import asdict
from models import Event, EventType
from event_bus import EventBus


class Auditor:
    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("auditor")
        self.log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.join(self.log_dir, "trades.jsonl")
        # Stats
        self.total_trades = 0
        self.winning_trades = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        self.peak_value = 0.0
        self.events_processed = 0
        self.recent_trades: list[dict] = []
        self.recent_signals: dict[str, dict] = {}  # symbol -> {indicator: signal_data}

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            self.events_processed += 1
            self._log_event(event)

            if event.type == EventType.ORDER_FILLED:
                self._on_trade(event.payload)
            elif event.type == EventType.PORTFOLIO_UPDATE:
                self._on_portfolio(event.payload)
            elif event.type in (EventType.TECHNICAL_SIGNAL, EventType.SENTIMENT_SIGNAL):
                self._on_signal(event.payload)

    def _log_event(self, event: Event):
        try:
            record = {
                "type": event.type.value,
                "source": event.source,
                "timestamp": event.timestamp,
                "payload": event.payload,
            }
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    def _on_trade(self, payload: dict):
        self.total_trades += 1
        pnl = payload.get("pnl", 0)
        if pnl > 0:
            self.winning_trades += 1
        self.total_pnl += pnl
        self.recent_trades.append(payload)
        if len(self.recent_trades) > 20:
            self.recent_trades.pop(0)

    def _on_portfolio(self, payload: dict):
        value = payload.get("total_value", 0)
        if value > self.peak_value:
            self.peak_value = value
        if self.peak_value > 0:
            dd = (self.peak_value - value) / self.peak_value
            if dd > self.max_drawdown:
                self.max_drawdown = dd

    def _on_signal(self, payload: dict):
        symbol = payload.get("symbol", "")
        indicator = payload.get("indicator", "")
        if symbol not in self.recent_signals:
            self.recent_signals[symbol] = {}
        self.recent_signals[symbol][indicator] = payload

    def get_summary(self) -> dict:
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        avg_pnl = (self.total_pnl / self.total_trades) if self.total_trades > 0 else 0
        return {
            "total_trades": self.total_trades,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "max_drawdown": round(self.max_drawdown * 100, 1),
            "events_processed": self.events_processed,
            "recent_trades": self.recent_trades[-10:],
            "recent_signals": self.recent_signals,
        }
