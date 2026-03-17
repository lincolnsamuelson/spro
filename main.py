import asyncio
import signal
import sys
from config import load_config
from event_bus import EventBus
from agents.market_data import MarketDataCollector
from agents.technical import TechnicalAnalyst
from agents.sentiment import SentimentAnalyst
from agents.strategy import StrategyEngine
from agents.risk import RiskManager
from agents.executor import Executor
from agents.auditor import Auditor
from agents.evaluator import Evaluator
from dashboard import Dashboard


async def main():
    config = load_config()
    bus = EventBus()

    print("Starting Crypto Paper Trader...")
    print(f"  Mode: {config['trading']['mode']}")
    print(f"  Starting balance: ${config['trading']['starting_balance']:.2f}")
    print(f"  Pairs: {len(config['trading']['pairs'])} coins")
    print(f"  Poll interval: {config['market_data']['poll_interval_seconds']}s")
    print(f"  Max positions: unlimited")
    print(f"  Take profit: {config['risk']['default_take_profit_pct']*100}%")
    print(f"  Risk level: MEDIUM")
    print()
    print("Initializing agents...")

    market = MarketDataCollector(bus, config)
    technical = TechnicalAnalyst(bus, config)
    sentiment = SentimentAnalyst(bus, config)
    strategy = StrategyEngine(bus, config)
    risk = RiskManager(bus, config)
    executor = Executor(bus, config)
    auditor = Auditor(bus, config)
    evaluator = Evaluator(bus, config)
    dash = Dashboard(executor, auditor, evaluator, config)

    print("All 9 agents online. Fetching market data...\n")

    tasks = [
        asyncio.create_task(market.run(), name="market_data"),
        asyncio.create_task(technical.run(), name="technical"),
        asyncio.create_task(sentiment.run(), name="sentiment"),
        asyncio.create_task(strategy.run(), name="strategy"),
        asyncio.create_task(risk.run(), name="risk"),
        asyncio.create_task(executor.run(), name="executor"),
        asyncio.create_task(auditor.run(), name="auditor"),
        asyncio.create_task(evaluator.run(), name="evaluator"),
        asyncio.create_task(dash.run(), name="dashboard"),
    ]

    loop = asyncio.get_event_loop()

    def shutdown_handler():
        print("\n\nShutting down gracefully...")
        asyncio.create_task(bus.shutdown())

    loop.add_signal_handler(signal.SIGINT, shutdown_handler)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass

    print("All agents stopped. Final portfolio state saved.")


if __name__ == "__main__":
    asyncio.run(main())
