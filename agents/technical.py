import asyncio
from collections import defaultdict, deque
from models import Event, EventType, Signal, Side
from event_bus import EventBus
import indicators


class TechnicalAnalyst:
    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("technical")
        self.closes: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        self.ind_cfg = config["indicators"]

    async def run(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return
            if event.type == EventType.CANDLE:
                await self._on_candle(event.payload)

    async def _on_candle(self, payload: dict):
        symbol = payload["symbol"]
        self.closes[symbol].append(payload["close"])
        closes = list(self.closes[symbol])

        await self._check_rsi(symbol, closes)
        await self._check_macd(symbol, closes)
        await self._check_bollinger(symbol, closes)
        await self._check_ema(symbol, closes)

    async def _check_rsi(self, symbol: str, closes: list):
        val = indicators.rsi(closes, self.ind_cfg["rsi_period"])
        if val is None:
            return
        if val < 30:
            strength = (30 - val) / 30
            await self._emit(symbol, "rsi", Side.BUY, strength)
        elif val > 70:
            strength = (val - 70) / 30
            await self._emit(symbol, "rsi", Side.SELL, min(strength, 1.0))

    async def _check_macd(self, symbol: str, closes: list):
        result = indicators.macd(
            closes,
            self.ind_cfg["macd_fast"],
            self.ind_cfg["macd_slow"],
            self.ind_cfg["macd_signal"],
        )
        if result is None:
            return
        _, _, histogram = result
        # Check for histogram crossing zero by looking at recent closes
        if len(closes) < self.ind_cfg["macd_slow"] + self.ind_cfg["macd_signal"] + 1:
            return
        prev_result = indicators.macd(
            closes[:-1],
            self.ind_cfg["macd_fast"],
            self.ind_cfg["macd_slow"],
            self.ind_cfg["macd_signal"],
        )
        if prev_result is None:
            return
        _, _, prev_hist = prev_result
        if prev_hist <= 0 and histogram > 0:
            await self._emit(symbol, "macd", Side.BUY, min(abs(histogram) * 10, 1.0))
        elif prev_hist >= 0 and histogram < 0:
            await self._emit(symbol, "macd", Side.SELL, min(abs(histogram) * 10, 1.0))

    async def _check_bollinger(self, symbol: str, closes: list):
        result = indicators.bollinger_bands(
            closes, self.ind_cfg["bb_period"], self.ind_cfg["bb_std"]
        )
        if result is None:
            return
        upper, middle, lower = result
        price = closes[-1]
        band_width = upper - lower
        if band_width == 0:
            return
        if price <= lower:
            strength = min((lower - price) / band_width + 0.5, 1.0)
            await self._emit(symbol, "bollinger", Side.BUY, strength)
        elif price >= upper:
            strength = min((price - upper) / band_width + 0.5, 1.0)
            await self._emit(symbol, "bollinger", Side.SELL, strength)

    async def _check_ema(self, symbol: str, closes: list):
        cross = indicators.ema_crossover(
            closes, self.ind_cfg["ema_fast"], self.ind_cfg["ema_slow"]
        )
        if cross == "golden_cross":
            await self._emit(symbol, "ema_crossover", Side.BUY, 0.7)
        elif cross == "death_cross":
            await self._emit(symbol, "ema_crossover", Side.SELL, 0.7)

    async def _emit(self, symbol: str, indicator: str, direction: Side, strength: float):
        signal = Signal(
            symbol=symbol,
            indicator=indicator,
            direction=direction,
            strength=strength,
        )
        await self.bus.publish(Event(
            type=EventType.TECHNICAL_SIGNAL,
            payload={
                "symbol": signal.symbol,
                "indicator": signal.indicator,
                "direction": signal.direction.value,
                "strength": signal.strength,
            },
            source="technical",
        ))
