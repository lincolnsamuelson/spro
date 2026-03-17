from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any
import time


class EventType(Enum):
    PRICE_UPDATE = "price_update"
    CANDLE = "candle"
    TECHNICAL_SIGNAL = "technical_signal"
    SENTIMENT_SIGNAL = "sentiment_signal"
    TRADE_SIGNAL = "trade_signal"
    ORDER_REQUEST = "order_request"
    ORDER_FILLED = "order_filled"
    PORTFOLIO_UPDATE = "portfolio_update"
    STRATEGY_ADJUSTMENT = "strategy_adjustment"
    HEARTBEAT = "heartbeat"
    SHUTDOWN = "shutdown"


@dataclass
class Event:
    type: EventType
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    source: str = ""


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Candle:
    symbol: str
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    symbol: str
    indicator: str
    direction: Side
    strength: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: float
    price: float
    confidence: float
    stop_loss: float
    take_profit: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class Position:
    symbol: str
    side: Side
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    unrealized_pnl: float = 0.0


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    total_value: float = 0.0
    realized_pnl: float = 0.0
    peak_value: float = 0.0
