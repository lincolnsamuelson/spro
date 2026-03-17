from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List
import time


class EventType(Enum):
    PRICE_UPDATE = "price_update"
    CANDLE = "candle"
    TECHNICAL_SIGNAL = "technical_signal"
    SENTIMENT_SIGNAL = "sentiment_signal"
    MOMENTUM_SIGNAL = "momentum_signal"
    VOLATILITY_RANKING = "volatility_ranking"
    TRADE_SIGNAL = "trade_signal"
    ORDER_REQUEST = "order_request"
    ORDER_FILLED = "order_filled"
    POSITION_CLOSED = "position_closed"
    PORTFOLIO_UPDATE = "portfolio_update"
    STRATEGY_ADJUSTMENT = "strategy_adjustment"
    COMPOUND_TRIGGER = "compound_trigger"
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
    leverage: int = 1
    timestamp: float = field(default_factory=time.time)


@dataclass
class Position:
    symbol: str
    side: Side
    entry_price: float
    quantity: float
    leverage: int
    stop_loss: float
    trailing_stop: float
    highest_price: float
    unrealized_pnl: float = 0.0
    margin: float = 0.0  # actual capital locked


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    total_value: float = 0.0
    realized_pnl: float = 0.0
    peak_value: float = 0.0
    win_streak: int = 0
    total_wins: int = 0
    total_losses: int = 0
