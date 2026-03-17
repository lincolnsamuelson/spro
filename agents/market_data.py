import asyncio
import json
import time
from collections import defaultdict, deque
import websockets
from models import Event, EventType, Candle
from event_bus import EventBus

# Map CoinGecko IDs to Binance ticker symbols
COINGECKO_TO_BINANCE = {
    "bitcoin": "btcusdt", "ethereum": "ethusdt", "ripple": "xrpusdt",
    "binancecoin": "bnbusdt", "solana": "solusdt", "tron": "trxusdt",
    "dogecoin": "dogeusdt", "cardano": "adausdt", "bitcoin-cash": "bchusdt",
    "chainlink": "linkusdt", "monero": "xmrusdt", "stellar": "xlmusdt",
    "avalanche-2": "avaxusdt", "litecoin": "ltcusdt", "hedera-hashgraph": "hbarusdt",
    "sui": "suiusdt", "shiba-inu": "shibusdt", "the-open-network": "tonusdt",
    "polkadot": "dotusdt", "uniswap": "uniusdt", "near": "nearusdt",
    "aave": "aaveusdt", "pepe": "pepeusdt", "internet-computer": "icpusdt",
    "ethereum-classic": "etcusdt", "ondo-finance": "ondousdt",
    "polygon-ecosystem-token": "polusdt", "render-token": "renderusdt",
    "cosmos": "atomusdt", "kaspa": "kasusdt", "algorand": "algousdt",
    "aptos": "aptusdt", "filecoin": "filusdt", "vechain": "vetusdt",
    "arbitrum": "arbusdt", "bonk": "bonkusdt", "fetch-ai": "fetusdt",
    "virtual-protocol": "virtualusdt", "official-trump": "trumpusdt",
    "jupiter-exchange-solana": "jupusdt", "okb": "okbusdt",
    "flare-networks": "flrusdt", "morpho": "morphousdt",
    "worldcoin-wld": "wldusdt", "bittensor": "taousdt",
    "mantle": "mntusdt", "ethena": "enausdt", "quant-network": "qntusdt",
    "hyperliquid": "hypeusdt", "crypto-com-chain": "crousdt",
    "pi-network": "piusdt",
}

# Reverse map: binance symbol -> coingecko id
BINANCE_TO_COINGECKO = {v: k for k, v in COINGECKO_TO_BINANCE.items()}


class MarketDataCollector:
    """Streams real-time prices from Binance websockets (~100ms updates).
    Falls back to CoinGecko REST polling for coins not on Binance."""

    def __init__(self, bus: EventBus, config: dict):
        self.bus = bus
        self.config = config
        self.queue = bus.subscribe("market_data")
        self.candle_interval = config["market_data"]["candle_interval_seconds"]
        self.pairs = config["trading"]["pairs"]
        self.prices: dict[str, float] = {}
        self.price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        self._candle_data: dict[str, dict] = {}
        self.ticks_received: int = 0

        # Split pairs into Binance-streamable and fallback
        self.binance_pairs = []
        self.fallback_pairs = []
        for pair in self.pairs:
            if pair in COINGECKO_TO_BINANCE:
                self.binance_pairs.append(pair)
            else:
                self.fallback_pairs.append(pair)

    async def run(self):
        tasks = [
            asyncio.create_task(self._binance_stream()),
            asyncio.create_task(self._drain_queue()),
        ]
        # Fallback polling for coins not on Binance
        if self.fallback_pairs:
            tasks.append(asyncio.create_task(self._fallback_poll()))
        await asyncio.gather(*tasks)

    async def _drain_queue(self):
        while True:
            event = await self.queue.get()
            if event.type == EventType.SHUTDOWN:
                return

    async def _binance_stream(self):
        """Connect to Binance combined websocket stream for all pairs at once."""
        if not self.binance_pairs:
            return

        # Build stream names: symbol@miniTicker for each pair
        streams = [f"{COINGECKO_TO_BINANCE[p]}@miniTicker" for p in self.binance_pairs]
        # Binance allows up to 1024 streams in a combined connection
        url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            payload = data.get("data", {})
                            binance_sym = payload.get("s", "").lower() + "usdt"
                            # miniTicker uses 's' for symbol without 'usdt' suffix sometimes
                            # Actually the stream name format means we get the full symbol
                            stream_name = data.get("stream", "")
                            binance_ticker = stream_name.split("@")[0] if "@" in stream_name else ""

                            if not binance_ticker:
                                continue

                            coingecko_id = BINANCE_TO_COINGECKO.get(binance_ticker)
                            if not coingecko_id:
                                continue

                            price = float(payload.get("c", 0))  # 'c' = close/last price
                            if price <= 0:
                                continue

                            now = time.time()
                            self.prices[coingecko_id] = price
                            self.price_history[coingecko_id].append((now, price))
                            self.ticks_received += 1

                            await self.bus.publish(Event(
                                type=EventType.PRICE_UPDATE,
                                payload={"symbol": coingecko_id, "price": price, "timestamp": now},
                                source="market_data",
                            ))

                            self._update_candle(coingecko_id, now, price)

                        except (json.JSONDecodeError, KeyError, ValueError):
                            continue

            except (websockets.ConnectionClosed, ConnectionError, OSError):
                await asyncio.sleep(2)
                continue
            except asyncio.CancelledError:
                return

    async def _fallback_poll(self):
        """Poll CoinGecko for coins not available on Binance."""
        import aiohttp
        backoff = 1
        api_base = self.config["market_data"]["api_base"]
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    ids = ",".join(self.fallback_pairs)
                    url = f"{api_base}/simple/price?ids={ids}&vs_currencies=usd"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 429:
                            backoff = min(backoff * 2, 60)
                            await asyncio.sleep(backoff)
                            continue
                        resp.raise_for_status()
                        data = await resp.json()
                        backoff = 1

                    now = time.time()
                    for pair in self.fallback_pairs:
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

                except Exception:
                    backoff = min(backoff * 2, 60)
                    await asyncio.sleep(backoff)
                    continue
                except asyncio.CancelledError:
                    return

                await asyncio.sleep(30)

    def _update_candle(self, symbol: str, now: float, price: float):
        bucket = int(now // self.candle_interval) * self.candle_interval

        if symbol not in self._candle_data or self._candle_data[symbol]["bucket"] != bucket:
            if symbol in self._candle_data:
                cd = self._candle_data[symbol]
                asyncio.create_task(self.bus.publish(Event(
                    type=EventType.CANDLE,
                    payload={
                        "symbol": symbol,
                        "timestamp": cd["bucket"],
                        "open": cd["open"],
                        "high": cd["high"],
                        "low": cd["low"],
                        "close": cd["close"],
                    },
                    source="market_data",
                )))
            self._candle_data[symbol] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
        else:
            cd = self._candle_data[symbol]
            cd["high"] = max(cd["high"], price)
            cd["low"] = min(cd["low"], price)
            cd["close"] = price
