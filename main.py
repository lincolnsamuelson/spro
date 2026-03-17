import asyncio
import signal
from config import load_config
from event_bus import EventBus
from agents.market_data import MarketDataCollector
from agents.technical import TechnicalAnalyst
from agents.sentiment import SentimentAnalyst
from agents.momentum import MomentumDetector
from agents.volatility import VolatilityScanner
from agents.strategy import StrategyEngine
from agents.risk import RiskManager
from agents.executor import Executor
from agents.auditor import Auditor
from agents.evaluator import Evaluator
from agents.compounder import Compounder
from dashboard import Dashboard


async def main():
    config = load_config()
    bus = EventBus()

    print("=" * 60)
    print("  AGGRESSIVE LEVERAGED CRYPTO TRADER")
    print("=" * 60)
    print(f"  Mode:       PAPER (simulated)")
    print(f"  Balance:    ${config['trading']['starting_balance']:.2f}")
    print(f"  Coins:      {len(config['trading']['pairs'])}")
    print(f"  Leverage:   {config['trading']['default_leverage']}x (max {config['trading']['max_leverage']}x)")
    print(f"  Risk:       MAX — 25% per trade, 50% drawdown halt")
    print(f"  Trailing:   {config['risk']['trailing_stop_pct']*100}% trailing stop")
    print(f"  Positions:  UNLIMITED")
    print()
    print("  Agent Team:")
    print("    1. Market Data Collector    — 57 coins, 15s polling")
    print("    2. Technical Analyst        — RSI, MACD, BB, EMA")
    print("    3. Sentiment Analyst        — Fear/Greed Index")
    print("    4. Momentum Detector        — Breakouts, spikes, acceleration")
    print("    5. Volatility Scanner       — Ranks coins, sets leverage")
    print("    6. Strategy Engine          — Weighted signals, adaptive")
    print("    7. Risk Manager             — Aggressive sizing, leverage")
    print("    8. Executor                 — Leveraged paper trades + trailing stops")
    print("    9. Compounder               — Zero idle capital, streak scaling")
    print("   10. Evaluator                — Trade grading, strategy refinement")
    print("   11. Auditor                  — Full event logging")
    print("   12. Dashboard                — Live terminal UI")
    print()
    print("  Initializing 12 agents...")

    market = MarketDataCollector(bus, config)
    technical = TechnicalAnalyst(bus, config)
    sentiment = SentimentAnalyst(bus, config)
    momentum = MomentumDetector(bus, config)
    volatility = VolatilityScanner(bus, config)
    strategy = StrategyEngine(bus, config)
    risk = RiskManager(bus, config)
    executor = Executor(bus, config)
    auditor = Auditor(bus, config)
    evaluator = Evaluator(bus, config)
    compounder = Compounder(bus, config)
    dash = Dashboard(executor, auditor, evaluator, compounder, volatility, config)

    print("  All 12 agents online. LFG.\n")

    tasks = [
        asyncio.create_task(market.run(), name="market_data"),
        asyncio.create_task(technical.run(), name="technical"),
        asyncio.create_task(sentiment.run(), name="sentiment"),
        asyncio.create_task(momentum.run(), name="momentum"),
        asyncio.create_task(volatility.run(), name="volatility"),
        asyncio.create_task(strategy.run(), name="strategy"),
        asyncio.create_task(risk.run(), name="risk"),
        asyncio.create_task(executor.run(), name="executor"),
        asyncio.create_task(auditor.run(), name="auditor"),
        asyncio.create_task(evaluator.run(), name="evaluator"),
        asyncio.create_task(compounder.run(), name="compounder"),
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

    print("All agents stopped. Portfolio saved.")


if __name__ == "__main__":
    asyncio.run(main())
