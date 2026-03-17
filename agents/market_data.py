import asyncio
import time
from collections import defaultdict, deque
import aiohttp
from models import Event, EventType, Candle
from event_bus import EventBus


class MarketDataCollector:
    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("market_data")
        self.poll_interval = config["market_data"]["poll_interval_seconds"]
        self.candle_interval = config["market_data"]["candle_interval_seconds"]
        self.api_base = config["market_data"]["api_base"]
        self.pairs = config["trading"]["pairs"]
        self.prices: dict[str, float] = {}
        self.price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        self._candle_data: dict[str, dict] = {}

    async def run(self):
        fetch_task = asyncio.create_task(self._poll_loop())
        drain_task = asyncio.create_task(self._drain_queue())
        await asyncio.gather(fetch_task, drain_task)

    async def _drain_queue(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return

    async def _poll_loop(self):
        backoff = 1
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    ids = ",".join(self.pairs)
                    url = f"{self.api_base}/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 429:
                            backoff = min(backoff * 2, 60)
                            await asyncio.sleep(backoff)
                            continue
                        resp.raise_for_status()
                        data = await resp.json()
                        backoff = 1

                    now = time.time()
                    for pair in self.pairs:
                        if pair in data and "usd" in data[pair]:
                            price = float(data[pair]["usd"])
                            self.prices[pair] = price
                            self.price_history[pair].append((now, price))

                            await self.bus.publish(Event(
                                type=EventType.PRICE_UPDATE,
                                payload={"symbol": pair, "price": price, "timestamp": now},
                                source="market_data",
                            ))

                            self._update_candle(pair, now, price)

                except aiohttp.ClientError:
                    backoff = min(backoff * 2, 60)
                    await asyncio.sleep(backoff)
                    continue
                except asyncio.CancelledError:
                    return

                await asyncio.sleep(self.poll_interval)

    def _update_candle(self, symbol: str, now: float, price: float):
        bucket = int(now // self.candle_interval) * self.candle_interval
        key = symbol

        if key not in self._candle_data or self._candle_data[key]["bucket"] != bucket:
            # Close previous candle if exists
            if key in self._candle_data:
                cd = self._candle_data[key]
                candle = Candle(
                    symbol=symbol,
                    timestamp=cd["bucket"],
                    open=cd["open"],
                    high=cd["high"],
                    low=cd["low"],
                    close=cd["close"],
                    volume=0.0,
                )
                asyncio.create_task(self.bus.publish(Event(
                    type=EventType.CANDLE,
                    payload={
                        "symbol": candle.symbol,
                        "timestamp": candle.timestamp,
                        "open": candle.open,
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                    },
                    source="market_data",
                )))
            # Start new candle
            self._candle_data[key] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
        else:
            cd = self._candle_data[key]
            cd["high"] = max(cd["high"], price)
            cd["low"] = min(cd["low"], price)
            cd["close"] = price
