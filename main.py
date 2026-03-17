import asyncio
import signal
from config import load_config
from event_bus import EventBus
from agents.market_data import MarketDataCollector
from agents.technical import TechnicalAnalyst
from agents.sentiment import SentimentAnalyst
from agents.momentum import MomentumDetector
from agents.volatility import VolatilityScanner
from agents.order_flow import OrderFlowAnalyst
from agents.correlation import CorrelationAnalyst
from agents.microstructure import MicrostructureAnalyst
from agents.trend_researcher import TrendResearcher
from agents.signal_router import SignalRouter
from agents.trader_agent import TraderAgent
from agents.portfolio_manager import PortfolioManager
from agents.auditor import Auditor
from agents.evaluator import Evaluator
from agents.compounder import Compounder
from dashboard import Dashboard


# 20 competitors — each starts with equal capital, same rules, different strategies
TRADER_POOL = [
    {"id": "luna",     "style": "momentum_chaser",  "max_pos": 6},
    {"id": "nova",     "style": "breakout_hunter",   "max_pos": 5},
    {"id": "aria",     "style": "scalper",            "max_pos": 7},
    {"id": "jade",     "style": "mean_reverter",      "max_pos": 4},
    {"id": "ruby",     "style": "sentiment_rider",    "max_pos": 5},
    {"id": "stella",   "style": "momentum_chaser",  "max_pos": 8},
    {"id": "ivy",      "style": "breakout_hunter",   "max_pos": 6},
    {"id": "pearl",    "style": "scalper",            "max_pos": 5},
    {"id": "sage",     "style": "mean_reverter",      "max_pos": 4},
    {"id": "aurora",   "style": "sentiment_rider",    "max_pos": 6},
    {"id": "ember",    "style": "momentum_chaser",  "max_pos": 7},
    {"id": "violet",   "style": "breakout_hunter",   "max_pos": 5},
    {"id": "storm",    "style": "scalper",            "max_pos": 8},
    {"id": "raven",    "style": "mean_reverter",      "max_pos": 4},
    {"id": "phoenix",  "style": "sentiment_rider",    "max_pos": 6},
    {"id": "celeste",  "style": "momentum_chaser",  "max_pos": 5},
    {"id": "siren",    "style": "breakout_hunter",   "max_pos": 7},
    {"id": "venom",    "style": "scalper",            "max_pos": 6},
    {"id": "nyx",      "style": "mean_reverter",      "max_pos": 4},
    {"id": "echo",     "style": "sentiment_rider",    "max_pos": 5},
]


async def main():
    config = load_config()
    bus = EventBus()
    per_trader = config["trading"]["starting_balance"]  # $50 each

    print("=" * 70)
    print(f"  TRADER COMPETITION — 20 Competitors x ${per_trader:.0f} Each")
    print("=" * 70)
    print(f"  Mode:       PAPER (simulated)")
    print(f"  Per Trader: ${per_trader:.2f}")
    print(f"  Total Pool: ${per_trader * len(TRADER_POOL):.2f}")
    print(f"  Coins:      {len(config['trading']['pairs'])}")
    print(f"  Leverage:   {config['trading']['default_leverage']}x (max {config['trading']['max_leverage']}x)")
    print()

    # === Competition Portfolio Manager ===
    portfolio_mgr = PortfolioManager(bus, config, trader_configs=TRADER_POOL)

    # === 20 Competing Trader Agents ===
    traders = []
    print("  Competitors:")
    for tc in TRADER_POOL:
        trader = TraderAgent(
            trader_id=tc["id"],
            bus=bus,
            config=config,
            portfolio_mgr=portfolio_mgr,
            style=tc["style"],
            capital=per_trader,
            max_positions=tc["max_pos"],
        )
        traders.append(trader)
        print(f"    {tc['id']:18s}  {tc['style']:18s}  ${per_trader:>6.2f}  max {tc['max_pos']} pos")
    print()

    # === Signal Router ===
    router = SignalRouter(bus, config, traders)

    # === 10 Researcher Agents (shared advisors) ===
    print("  Research Advisors:")
    market = MarketDataCollector(bus, config)
    print(f"    1.  Market Data        — Binance WS + CoinGecko fallback")

    tech_fast = TechnicalAnalyst(bus, config, timeframe="fast")
    tech_std = TechnicalAnalyst(bus, config, timeframe="standard")
    tech_slow = TechnicalAnalyst(bus, config, timeframe="slow")
    print(f"    2-4. Technical x3      — fast/standard/slow timeframes")

    sentiment = SentimentAnalyst(bus, config)
    print(f"    5.  Sentiment          — Fear & Greed Index")

    mom_high = MomentumDetector(bus, config, sensitivity="high")
    mom_std = MomentumDetector(bus, config, sensitivity="standard")
    print(f"    6-7. Momentum x2       — high/standard sensitivity")

    volatility = VolatilityScanner(bus, config)
    print(f"    8.  Volatility         — Ranks coins, sets leverage")

    order_flow = OrderFlowAnalyst(bus, config)
    print(f"    9.  Order Flow         — Buy/sell pressure detection")

    correlation = CorrelationAnalyst(bus, config)
    print(f"   10.  Correlation        — BTC/alt divergence signals")

    microstructure = MicrostructureAnalyst(bus, config)
    print(f"   11.  Microstructure     — Round numbers, tick spikes")

    trend = TrendResearcher(bus, config)
    print(f"   12.  Trend Researcher   — Identifies rising coins & inflection points")
    print()

    # === Support Agents ===
    auditor = Auditor(bus, config)
    evaluator = Evaluator(bus, config)
    compounder = Compounder(bus, config)
    dash = Dashboard(portfolio_mgr, auditor, evaluator, compounder,
                     volatility, traders, config)

    print(f"  Support: Portfolio Manager, Auditor, Evaluator, Compounder, Dashboard")
    print(f"  Total agents: {12 + len(traders) + 5}")
    print()
    print("  COMPETITION ACTIVE. May the best trader win.\n")

    # === Build task list ===
    tasks = [
        asyncio.create_task(market.run(), name="market_data"),
        asyncio.create_task(tech_fast.run(), name="technical_fast"),
        asyncio.create_task(tech_std.run(), name="technical_standard"),
        asyncio.create_task(tech_slow.run(), name="technical_slow"),
        asyncio.create_task(sentiment.run(), name="sentiment"),
        asyncio.create_task(mom_high.run(), name="momentum_high"),
        asyncio.create_task(mom_std.run(), name="momentum_standard"),
        asyncio.create_task(volatility.run(), name="volatility"),
        asyncio.create_task(order_flow.run(), name="order_flow"),
        asyncio.create_task(correlation.run(), name="correlation"),
        asyncio.create_task(microstructure.run(), name="microstructure"),
        asyncio.create_task(trend.run(), name="trend_researcher"),
        asyncio.create_task(router.run(), name="signal_router"),
        asyncio.create_task(portfolio_mgr.run(), name="portfolio_manager"),
        asyncio.create_task(auditor.run(), name="auditor"),
        asyncio.create_task(evaluator.run(), name="evaluator"),
        asyncio.create_task(compounder.run(), name="compounder"),
        asyncio.create_task(dash.run(), name="dashboard"),
    ]
    for trader in traders:
        tasks.append(asyncio.create_task(trader.run(), name=f"trader_{trader.trader_id}"))

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
