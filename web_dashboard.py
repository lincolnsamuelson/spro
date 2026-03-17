"""
Crypto Trader Web Dashboard
Run: python web_dashboard.py
Open: http://localhost:8080
"""

import json
import time
import os
from pathlib import Path
from aiohttp import web, ClientSession

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "logs" / "trades.jsonl"
PORTFOLIO_FILE = BASE_DIR / "portfolio.json"
CONFIG_FILE = BASE_DIR / "config.json"

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

COIN_IDS = CONFIG["trading"]["pairs"]


def load_portfolio():
    with open(PORTFOLIO_FILE) as f:
        return json.load(f)


def parse_logs():
    trades = []
    portfolio_snapshots = []
    signals = []
    latest_prices = {}
    price_ticks = []  # all price updates for chart

    with open(LOG_FILE) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            t = entry.get("type")
            if t == "order_filled":
                trades.append({**entry["payload"], "timestamp": entry["timestamp"]})
            elif t == "portfolio_update":
                portfolio_snapshots.append({**entry["payload"], "timestamp": entry["timestamp"]})
            elif t in ("technical_signal", "sentiment_signal", "trade_signal"):
                signals.append({**entry["payload"], "timestamp": entry["timestamp"], "signal_type": t})
            elif t == "price_update":
                p = entry["payload"]
                latest_prices[p["symbol"]] = p["price"]
                price_ticks.append({
                    "symbol": p["symbol"],
                    "price": p["price"],
                    "timestamp": entry["timestamp"],
                })

    return trades, portfolio_snapshots, signals, latest_prices, price_ticks


def build_chart_series(trades, price_ticks, portfolio_snapshots, starting_balance):
    """Build stacked area chart data: cash + per-position value over time."""
    if not price_ticks:
        return {"labels": [], "datasets": []}

    # Figure out when each position was opened/closed
    # positions_held: {symbol: {quantity, entry_price, open_time}}
    positions_held = {}
    trade_events = []
    for t in trades:
        ts = t.get("time", t.get("timestamp", 0))
        side = t["side"].upper()
        sym = t["symbol"]
        qty = t["quantity"]
        price = t["price"]
        if side == "BUY":
            positions_held[sym] = {"quantity": qty, "entry_price": price, "open_time": ts}
        elif side == "SELL" and sym in positions_held:
            del positions_held[sym]
        trade_events.append({"timestamp": ts, "symbol": sym, "side": side})

    # Collect all unique symbols ever held
    held_symbols = set()
    for t in trades:
        if t["side"].upper() == "BUY":
            held_symbols.add(t["symbol"])

    if not held_symbols:
        # No trades yet — just show cash as flat line
        first_ts = price_ticks[0]["timestamp"]
        last_ts = price_ticks[-1]["timestamp"]
        return {
            "labels": [first_ts * 1000, last_ts * 1000],
            "datasets": [{
                "label": "Cash",
                "data": [starting_balance, starting_balance],
                "color": "#334155",
            }],
        }

    # Build price lookup: group price ticks by timestamp bucket
    # Sample every ~60s to keep chart manageable
    all_timestamps = sorted(set(int(pt["timestamp"]) for pt in price_ticks))
    # Deduplicate to ~1 point per minute
    sampled_ts = []
    last_t = 0
    for ts in all_timestamps:
        if ts - last_t >= 30:
            sampled_ts.append(ts)
            last_t = ts
    if not sampled_ts:
        sampled_ts = all_timestamps[:1]

    # Build running price map from ticks
    price_map = {}  # symbol -> latest price at each point
    tick_idx = 0
    current_prices = {}

    # Pre-sort ticks
    sorted_ticks = sorted(price_ticks, key=lambda x: x["timestamp"])

    # Build per-position value series and cash series
    # Reconstruct portfolio state at each sampled timestamp
    cash_series = []
    position_series = {sym: [] for sym in held_symbols}

    # Replay: track when buys/sells happen
    trade_idx = 0
    sorted_trades = sorted(trades, key=lambda t: t.get("time", t.get("timestamp", 0)))
    active_positions = {}  # sym -> {quantity, entry_price}
    current_cash = starting_balance
    tick_ptr = 0

    for ts in sampled_ts:
        # Apply any trades that happened up to this timestamp
        while trade_idx < len(sorted_trades):
            t = sorted_trades[trade_idx]
            t_ts = t.get("time", t.get("timestamp", 0))
            if t_ts <= ts:
                side = t["side"].upper()
                sym = t["symbol"]
                if side == "BUY":
                    cost = t.get("cost", t["quantity"] * t["price"])
                    current_cash -= cost
                    active_positions[sym] = {"quantity": t["quantity"], "entry_price": t["price"]}
                elif side == "SELL":
                    if sym in active_positions:
                        proceeds = t["quantity"] * t["price"]
                        current_cash += proceeds
                        del active_positions[sym]
                trade_idx += 1
            else:
                break

        # Update prices from ticks up to this timestamp
        while tick_ptr < len(sorted_ticks) and sorted_ticks[tick_ptr]["timestamp"] <= ts:
            tick = sorted_ticks[tick_ptr]
            current_prices[tick["symbol"]] = tick["price"]
            tick_ptr += 1

        cash_series.append(round(max(current_cash, 0), 2))
        for sym in held_symbols:
            if sym in active_positions:
                price = current_prices.get(sym, active_positions[sym]["entry_price"])
                val = active_positions[sym]["quantity"] * price
                position_series[sym].append(round(val, 2))
            else:
                position_series[sym].append(0)

    # Build datasets
    datasets = [{"label": "Cash", "data": cash_series, "color": "#334155"}]
    sym_colors = {}
    color_idx = 0
    chart_colors = ['#3b82f6', '#8b5cf6', '#ec4899', '#f97316', '#22c55e',
                    '#eab308', '#06b6d4', '#ef4444', '#10b981', '#6366f1']
    for sym in sorted(held_symbols):
        c = chart_colors[color_idx % len(chart_colors)]
        color_idx += 1
        datasets.append({
            "label": sym.upper(),
            "data": position_series[sym],
            "color": c,
        })

    return {
        "labels": [ts * 1000 for ts in sampled_ts],  # JS milliseconds
        "datasets": datasets,
    }


async def fetch_live_prices(session):
    ids = ",".join(COIN_IDS)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return None


async def api_data(request):
    portfolio = load_portfolio()
    trades, snapshots, signals, log_prices, price_ticks = parse_logs()

    session = ClientSession()
    live = await fetch_live_prices(session)
    await session.close()

    prices = {}
    # Always start with log prices as fallback
    for coin_id, price in log_prices.items():
        prices[coin_id] = {"price": price, "change_24h": 0}
    # Overlay live prices if available
    if live:
        for coin_id, data in live.items():
            prices[coin_id] = {
                "price": data.get("usd", 0),
                "change_24h": data.get("usd_24h_change", 0),
            }

    # Calculate current portfolio value with live prices
    total_value = portfolio["cash"]
    positions_detail = []
    for sym, pos in portfolio.get("positions", {}).items():
        current_price = prices.get(sym, {}).get("price", pos["entry_price"])
        qty = pos["quantity"]
        entry = pos["entry_price"]
        market_value = current_price * qty
        unrealized_pnl = (current_price - entry) * qty
        pnl_pct = ((current_price - entry) / entry * 100) if entry else 0
        total_value += market_value
        positions_detail.append({
            "symbol": sym,
            "side": pos["side"],
            "quantity": qty,
            "entry_price": entry,
            "current_price": current_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "pnl_pct": pnl_pct,
            "stop_loss": pos.get("stop_loss", 0),
            "take_profit": pos.get("take_profit", 0),
        })

    starting = CONFIG["trading"]["starting_balance"]
    overall_pnl = total_value - starting
    overall_pnl_pct = (overall_pnl / starting * 100) if starting else 0
    drawdown = ((portfolio["peak_value"] - total_value) / portfolio["peak_value"] * 100) if portfolio["peak_value"] else 0

    # Trade history enrichment
    trade_history = []
    for t in trades:
        trade_history.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.get("time", t.get("timestamp", 0)))),
            "symbol": t["symbol"],
            "side": t["side"],
            "quantity": t["quantity"],
            "price": t["price"],
            "cost": t.get("cost", 0),
            "confidence": t.get("confidence", 0),
            "pnl": t.get("pnl"),
        })

    # Portfolio value over time
    value_history = []
    for s in snapshots:
        value_history.append({
            "time": s["timestamp"],
            "value": s["total_value"],
            "cash": s["cash"],
        })

    # Recent signals (last 50)
    recent_signals = []
    for s in signals[-50:]:
        recent_signals.append({
            "time": time.strftime("%H:%M:%S", time.localtime(s["timestamp"])),
            "symbol": s.get("symbol", ""),
            "type": s["signal_type"],
            "indicator": s.get("indicator", s.get("signal_type", "")),
            "direction": s.get("direction", ""),
            "strength": s.get("strength", s.get("confidence", 0)),
        })

    # Build chart series
    chart_data = build_chart_series(trades, price_ticks, snapshots, starting)

    return web.json_response({
        "chart": chart_data,
        "portfolio": {
            "total_value": round(total_value, 2),
            "cash": round(portfolio["cash"], 2),
            "realized_pnl": round(portfolio["realized_pnl"], 2),
            "overall_pnl": round(overall_pnl, 2),
            "overall_pnl_pct": round(overall_pnl_pct, 2),
            "peak_value": round(portfolio["peak_value"], 2),
            "drawdown": round(drawdown, 2),
            "starting_balance": starting,
        },
        "positions": positions_detail,
        "trades": trade_history,
        "prices": prices,
        "value_history": value_history,
        "recent_signals": recent_signals,
        "config": {
            "mode": CONFIG["trading"]["mode"],
            "max_positions": CONFIG["risk"]["max_positions"],
            "max_drawdown_pct": round(CONFIG["risk"]["max_drawdown"] * 100, 1),
            "stop_loss_pct": round(CONFIG["risk"].get("default_stop_loss_pct", 0.025) * 100, 1),
            "take_profit_pct": round(CONFIG["risk"].get("default_take_profit_pct", 0.05) * 100, 1),
        },
    })


async def index(request):
    return web.Response(text=HTML, content_type="text/html")


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Crypto Trader Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root {
    --bg: #0a0e17;
    --card: #111827;
    --border: #1e293b;
    --text: #e2e8f0;
    --text-dim: #64748b;
    --accent: #3b82f6;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #eab308;
    --orange: #f97316;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }
  .header {
    background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%);
    border-bottom: 1px solid var(--border);
    padding: 20px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .header h1 {
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.5px;
  }
  .header h1 span { color: var(--accent); }
  .header-meta {
    display: flex;
    gap: 16px;
    align-items: center;
    font-size: 12px;
    color: var(--text-dim);
  }
  .mode-badge {
    background: #1e3a5f;
    color: var(--accent);
    padding: 4px 10px;
    border-radius: 4px;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .live-dot {
    width: 8px; height: 8px;
    background: var(--green);
    border-radius: 50%;
    display: inline-block;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }
  .container { padding: 24px 32px; max-width: 1600px; margin: 0 auto; }

  /* KPI cards */
  .kpi-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .kpi {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
  }
  .kpi-label {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
  }
  .kpi-value {
    font-size: 28px;
    font-weight: 700;
  }
  .kpi-sub {
    font-size: 12px;
    margin-top: 4px;
  }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--text-dim); }

  /* Grid layout */
  .grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    margin-bottom: 24px;
  }
  .grid-full { margin-bottom: 24px; }

  /* Cards */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }
  .card-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
    font-weight: 600;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .card-body { padding: 16px 20px; }

  /* Tables */
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 10px 12px;
    font-size: 13px;
    border-bottom: 1px solid #1a2332;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1a2332; }

  .side-buy { color: var(--green); font-weight: 600; }
  .side-sell { color: var(--red); font-weight: 600; }

  /* Price grid */
  .price-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 8px;
    padding: 16px 20px;
  }
  .price-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    background: #0d1420;
    border-radius: 8px;
    font-size: 12px;
  }
  .price-item .sym {
    font-weight: 600;
    text-transform: uppercase;
    max-width: 70px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .price-item .val { font-weight: 500; }
  .price-item .chg { font-size: 11px; }

  /* Signals */
  .signal-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 0;
    font-size: 12px;
  }
  .signal-badge {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
  }
  .signal-buy { background: #14532d; color: var(--green); }
  .signal-sell { background: #7f1d1d; color: var(--red); }

  /* Progress bars */
  .progress-bar {
    height: 6px;
    background: #1e293b;
    border-radius: 3px;
    overflow: hidden;
    margin-top: 8px;
  }
  .progress-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.5s ease;
  }

  /* Chart area */
  .chart-container {
    padding: 20px;
    height: 280px;
    position: relative;
  }
  canvas { width: 100% !important; height: 100% !important; }

  /* Position bar */
  .pos-bar {
    display: flex;
    gap: 4px;
    margin-top: 8px;
  }
  .pos-segment {
    height: 8px;
    border-radius: 4px;
    transition: width 0.5s ease;
  }

  /* Responsive */
  @media (max-width: 900px) {
    .grid-2 { grid-template-columns: 1fr; }
    .kpi-row { grid-template-columns: repeat(2, 1fr); }
    .container { padding: 16px; }
  }

  .empty-state {
    text-align: center;
    padding: 40px;
    color: var(--text-dim);
    font-size: 13px;
  }
  .tooltip {
    font-size: 11px;
    color: var(--text-dim);
  }
  .risk-meter {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .risk-level {
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .risk-low { background: #14532d; color: var(--green); }
  .risk-medium { background: #713f12; color: var(--yellow); }
  .risk-high { background: #7f1d1d; color: var(--red); }

  .sparkline { display: flex; align-items: flex-end; gap: 1px; height: 30px; }
  .sparkline .bar {
    width: 3px;
    border-radius: 1px;
    transition: height 0.3s ease;
  }
</style>
</head>
<body>

<div class="header">
  <h1><span>CRYPTO</span> PAPER TRADER</h1>
  <div class="header-meta">
    <span class="live-dot"></span>
    <span id="last-updated">Loading...</span>
    <span class="mode-badge" id="mode-badge">PAPER</span>
  </div>
</div>

<div class="container">
  <!-- KPI Row -->
  <div class="kpi-row">
    <div class="kpi">
      <div class="kpi-label">Portfolio Value</div>
      <div class="kpi-value" id="kpi-value">--</div>
      <div class="kpi-sub" id="kpi-pnl">--</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Available Cash</div>
      <div class="kpi-value" id="kpi-cash">--</div>
      <div class="kpi-sub tooltip">Unallocated capital</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Realized P&L</div>
      <div class="kpi-value" id="kpi-realized">--</div>
      <div class="kpi-sub tooltip">From closed trades</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Max Drawdown</div>
      <div class="kpi-value" id="kpi-drawdown">--</div>
      <div class="progress-bar"><div class="progress-fill" id="dd-bar" style="width:0%; background:var(--green);"></div></div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Open Positions</div>
      <div class="kpi-value" id="kpi-positions">--</div>
      <div class="kpi-sub" id="kpi-max-pos">--</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Risk Level</div>
      <div class="kpi-value risk-meter" id="kpi-risk">
        <span class="risk-level risk-low">LOW</span>
      </div>
      <div class="kpi-sub tooltip" id="kpi-risk-detail">--</div>
    </div>
  </div>

  <!-- Portfolio Chart -->
  <div class="grid-full">
    <div class="card">
      <div class="card-header">
        Portfolio Value
        <span class="tooltip" id="chart-range">--</span>
      </div>
      <div class="chart-container" style="height: 340px;">
        <canvas id="portfolio-chart"></canvas>
      </div>
    </div>
  </div>

  <!-- Portfolio Allocation -->
  <div class="grid-full">
    <div class="card">
      <div class="card-header">
        Portfolio Allocation
        <span class="tooltip" id="alloc-summary">--</span>
      </div>
      <div class="card-body">
        <div class="pos-bar" id="alloc-bar"></div>
        <div id="alloc-legend" style="display:flex; gap:16px; margin-top:12px; flex-wrap:wrap; font-size:12px;"></div>
      </div>
    </div>
  </div>

  <!-- Positions & Trades -->
  <div class="grid-2">
    <div class="card">
      <div class="card-header">Open Positions</div>
      <div class="card-body" style="padding:0;">
        <table>
          <thead><tr>
            <th>Asset</th><th>Side</th><th>Entry</th><th>Current</th><th>Qty</th><th>P&L</th><th>SL / TP</th>
          </tr></thead>
          <tbody id="positions-body"></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="card-header">Trade History</div>
      <div class="card-body" style="padding:0;">
        <table>
          <thead><tr>
            <th>Time</th><th>Side</th><th>Asset</th><th>Price</th><th>Cost</th><th>Conf</th>
          </tr></thead>
          <tbody id="trades-body"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Prices & Signals -->
  <div class="grid-2">
    <div class="card">
      <div class="card-header">
        Live Prices
        <span class="tooltip" id="price-count">--</span>
      </div>
      <div class="price-grid" id="price-grid"></div>
    </div>
    <div class="card">
      <div class="card-header">Recent Signals</div>
      <div class="card-body" id="signals-body" style="max-height: 400px; overflow-y: auto;"></div>
    </div>
  </div>

  <!-- Strategy Config -->
  <div class="grid-full">
    <div class="card">
      <div class="card-header">Strategy & Risk Parameters</div>
      <div class="card-body" id="config-body" style="display: flex; gap: 32px; flex-wrap: wrap; font-size: 13px;"></div>
    </div>
  </div>
</div>

<script>
const COLORS = ['#3b82f6','#8b5cf6','#ec4899','#f97316','#22c55e','#eab308','#06b6d4','#ef4444','#10b981','#6366f1'];

function fmt(n, decimals=2) {
  if (n === null || n === undefined) return '--';
  return n.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
}

function fmtPrice(p) {
  if (p >= 1000) return '$' + fmt(p, 0);
  if (p >= 1) return '$' + fmt(p, 2);
  if (p >= 0.01) return '$' + fmt(p, 4);
  return '$' + p.toFixed(8);
}

function pnlClass(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'; }

let portfolioChart = null;

function renderChart(chartData) {
  const ctx = document.getElementById('portfolio-chart');
  if (!ctx) return;

  const labels = chartData.labels;
  const datasets = chartData.datasets.map((ds, i) => {
    const color = ds.color;
    // Parse hex to rgb for alpha
    const r = parseInt(color.slice(1,3), 16);
    const g = parseInt(color.slice(3,5), 16);
    const b = parseInt(color.slice(5,7), 16);
    return {
      label: ds.label,
      data: ds.data.map((v, j) => ({x: labels[j], y: v})),
      backgroundColor: `rgba(${r},${g},${b},0.45)`,
      borderColor: color,
      borderWidth: 1.5,
      fill: i === 0 ? 'origin' : '-1',
      pointRadius: 0,
      pointHitRadius: 8,
      tension: 0.3,
    };
  });

  if (portfolioChart) {
    portfolioChart.data.datasets = datasets;
    portfolioChart.update('none');
    return;
  }

  portfolioChart = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: 'index',
        intersect: false,
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: '#94a3b8',
            font: { family: "'SF Mono', monospace", size: 11 },
            usePointStyle: true,
            pointStyle: 'circle',
            padding: 16,
          },
        },
        tooltip: {
          backgroundColor: '#1e293b',
          titleColor: '#e2e8f0',
          bodyColor: '#e2e8f0',
          borderColor: '#334155',
          borderWidth: 1,
          padding: 12,
          bodyFont: { family: "'SF Mono', monospace", size: 12 },
          titleFont: { family: "'SF Mono', monospace", size: 12 },
          callbacks: {
            title: function(items) {
              if (!items.length) return '';
              return new Date(items[0].parsed.x).toLocaleString();
            },
            label: function(item) {
              return `  ${item.dataset.label}: $${item.parsed.y.toFixed(2)}`;
            },
            afterBody: function(items) {
              const total = items.reduce((sum, it) => sum + it.parsed.y, 0);
              return `\n  Total: $${total.toFixed(2)}`;
            },
          },
        },
      },
      scales: {
        x: {
          type: 'time',
          time: {
            tooltipFormat: 'MMM d, HH:mm',
            displayFormats: {
              minute: 'HH:mm',
              hour: 'HH:mm',
              day: 'MMM d',
            },
          },
          grid: { color: '#1e293b', drawBorder: false },
          ticks: { color: '#64748b', font: { family: "'SF Mono', monospace", size: 10 }, maxTicksLimit: 8 },
        },
        y: {
          stacked: true,
          grid: { color: '#1e293b', drawBorder: false },
          ticks: {
            color: '#64748b',
            font: { family: "'SF Mono', monospace", size: 10 },
            callback: function(v) { return '$' + v.toFixed(0); },
          },
        },
      },
    },
  });
}

async function refresh() {
  try {
    const res = await fetch('/api/data');
    const d = await res.json();
    render(d);
  } catch(e) {
    console.error('Refresh error:', e);
  }
}

function render(d) {
  const p = d.portfolio;

  // Portfolio chart
  if (d.chart && d.chart.labels.length > 1) {
    renderChart(d.chart);
    const firstDate = new Date(d.chart.labels[0]).toLocaleTimeString();
    const lastDate = new Date(d.chart.labels[d.chart.labels.length - 1]).toLocaleTimeString();
    document.getElementById('chart-range').textContent = `${firstDate} — ${lastDate}`;
  }

  // KPIs
  document.getElementById('kpi-value').textContent = '$' + fmt(p.total_value);
  const pnlEl = document.getElementById('kpi-pnl');
  pnlEl.textContent = `${p.overall_pnl >= 0 ? '+' : ''}$${fmt(p.overall_pnl)} (${p.overall_pnl >= 0 ? '+' : ''}${fmt(p.overall_pnl_pct)}%)`;
  pnlEl.className = 'kpi-sub ' + pnlClass(p.overall_pnl);

  document.getElementById('kpi-cash').textContent = '$' + fmt(p.cash);

  const realEl = document.getElementById('kpi-realized');
  realEl.textContent = '$' + fmt(p.realized_pnl);
  realEl.className = 'kpi-value ' + pnlClass(p.realized_pnl);

  document.getElementById('kpi-drawdown').textContent = fmt(p.drawdown) + '%';
  const ddBar = document.getElementById('dd-bar');
  const ddPct = Math.min(p.drawdown / d.config.max_drawdown_pct * 100, 100);
  ddBar.style.width = ddPct + '%';
  ddBar.style.background = p.drawdown < 5 ? 'var(--green)' : p.drawdown < 10 ? 'var(--yellow)' : 'var(--red)';

  document.getElementById('kpi-positions').textContent = d.positions.length;
  document.getElementById('kpi-max-pos').textContent = `of ${d.config.max_positions} max`;

  // Risk
  const riskEl = document.getElementById('kpi-risk');
  let riskLevel, riskClass;
  if (p.drawdown > 10 || d.positions.length >= d.config.max_positions) {
    riskLevel = 'HIGH'; riskClass = 'risk-high';
  } else if (p.drawdown > 5 || d.positions.length >= d.config.max_positions - 1) {
    riskLevel = 'MEDIUM'; riskClass = 'risk-medium';
  } else {
    riskLevel = 'LOW'; riskClass = 'risk-low';
  }
  riskEl.innerHTML = `<span class="risk-level ${riskClass}">${riskLevel}</span>`;
  document.getElementById('kpi-risk-detail').textContent =
    `DD: ${fmt(p.drawdown)}% / ${d.config.max_drawdown_pct}% max`;

  // Allocation bar
  const totalVal = p.total_value || 1;
  const bar = document.getElementById('alloc-bar');
  const legend = document.getElementById('alloc-legend');
  bar.innerHTML = '';
  legend.innerHTML = '';

  let segments = [];
  if (p.cash > 0) segments.push({label: 'Cash', value: p.cash, color: '#334155'});
  d.positions.forEach((pos, i) => {
    segments.push({
      label: pos.symbol.toUpperCase(),
      value: pos.market_value,
      color: COLORS[i % COLORS.length],
    });
  });
  segments.forEach(s => {
    const pct = (s.value / totalVal * 100);
    bar.innerHTML += `<div class="pos-segment" style="width:${pct}%;background:${s.color};"></div>`;
    legend.innerHTML += `<span style="display:flex;align-items:center;gap:4px;">
      <span style="width:10px;height:10px;border-radius:2px;background:${s.color};display:inline-block;"></span>
      ${s.label}: $${fmt(s.value)} (${fmt(pct, 1)}%)
    </span>`;
  });
  document.getElementById('alloc-summary').textContent =
    `${d.positions.length} position${d.positions.length !== 1 ? 's' : ''} | $${fmt(p.cash)} cash`;

  // Positions table
  const posBody = document.getElementById('positions-body');
  if (d.positions.length === 0) {
    posBody.innerHTML = '<tr><td colspan="7" class="empty-state">No open positions</td></tr>';
  } else {
    posBody.innerHTML = d.positions.map(pos => `
      <tr>
        <td style="font-weight:600;">${pos.symbol.toUpperCase()}</td>
        <td class="side-${pos.side}">${pos.side.toUpperCase()}</td>
        <td>${fmtPrice(pos.entry_price)}</td>
        <td>${fmtPrice(pos.current_price)}</td>
        <td>${fmt(pos.quantity, 4)}</td>
        <td class="${pnlClass(pos.unrealized_pnl)}">
          ${pos.unrealized_pnl >= 0 ? '+' : ''}$${fmt(pos.unrealized_pnl)}
          <br><span style="font-size:11px;">(${pos.pnl_pct >= 0 ? '+' : ''}${fmt(pos.pnl_pct)}%)</span>
        </td>
        <td style="font-size:11px;">
          <span class="negative">${fmtPrice(pos.stop_loss)}</span> /
          <span class="positive">${fmtPrice(pos.take_profit)}</span>
        </td>
      </tr>
    `).join('');
  }

  // Trades table
  const tradesBody = document.getElementById('trades-body');
  if (d.trades.length === 0) {
    tradesBody.innerHTML = '<tr><td colspan="6" class="empty-state">No trades yet</td></tr>';
  } else {
    tradesBody.innerHTML = d.trades.slice().reverse().map(t => `
      <tr>
        <td style="font-size:11px;">${t.time}</td>
        <td class="side-${t.side.toLowerCase()}">${t.side}</td>
        <td style="font-weight:600;">${t.symbol.toUpperCase()}</td>
        <td>${fmtPrice(t.price)}</td>
        <td>$${fmt(t.cost)}</td>
        <td>${fmt(t.confidence * 100, 0)}%</td>
      </tr>
    `).join('');
  }

  // Price grid
  const priceGrid = document.getElementById('price-grid');
  const priceEntries = Object.entries(d.prices).sort((a, b) => b[1].price - a[1].price);
  document.getElementById('price-count').textContent = `${priceEntries.length} coins`;

  priceGrid.innerHTML = priceEntries.map(([sym, data]) => {
    const chg = data.change_24h;
    const chgClass = chg > 0 ? 'positive' : chg < 0 ? 'negative' : 'neutral';
    const held = d.positions.some(p => p.symbol === sym);
    return `
      <div class="price-item" style="${held ? 'border: 1px solid var(--accent);' : ''}">
        <span class="sym">${sym.split('-')[0].toUpperCase()}</span>
        <span>
          <span class="val">${fmtPrice(data.price)}</span>
          <span class="chg ${chgClass}" style="margin-left:6px;">${chg >= 0 ? '+' : ''}${fmt(chg, 1)}%</span>
        </span>
      </div>
    `;
  }).join('');

  // Signals
  const sigBody = document.getElementById('signals-body');
  const sigs = d.recent_signals.slice().reverse();
  if (sigs.length === 0) {
    sigBody.innerHTML = '<div class="empty-state">Waiting for signals...</div>';
  } else {
    sigBody.innerHTML = sigs.map(s => `
      <div class="signal-row">
        <span style="color:var(--text-dim);width:60px;">${s.time}</span>
        <span style="font-weight:600;width:100px;">${s.symbol.split('-')[0].toUpperCase()}</span>
        <span class="signal-badge ${s.direction === 'buy' ? 'signal-buy' : 'signal-sell'}">
          ${s.direction.toUpperCase()}
        </span>
        <span style="width:100px;">${s.indicator.replace('_signal','').replace('technical_','')}</span>
        <span style="color:var(--text-dim);">str: ${typeof s.strength === 'number' ? fmt(s.strength, 2) : s.strength}</span>
      </div>
    `).join('');
  }

  // Config
  document.getElementById('config-body').innerHTML = `
    <div><span style="color:var(--text-dim);">Mode:</span> <strong>${d.config.mode.toUpperCase()}</strong></div>
    <div><span style="color:var(--text-dim);">Max Positions:</span> <strong>${d.config.max_positions}</strong></div>
    <div><span style="color:var(--text-dim);">Max Drawdown:</span> <strong>${d.config.max_drawdown_pct}%</strong></div>
    <div><span style="color:var(--text-dim);">Stop Loss:</span> <strong>${d.config.stop_loss_pct}%</strong></div>
    <div><span style="color:var(--text-dim);">Take Profit:</span> <strong>${d.config.take_profit_pct}%</strong></div>
    <div><span style="color:var(--text-dim);">Starting Balance:</span> <strong>$${p.starting_balance}</strong></div>
  `;

  // Mode badge
  document.getElementById('mode-badge').textContent = d.config.mode.toUpperCase();

  // Timestamp
  document.getElementById('last-updated').textContent =
    'Updated ' + new Date().toLocaleTimeString();
}

// Initial load + auto-refresh every 10s
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""

app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/api/data", api_data)

if __name__ == "__main__":
    print("Starting Crypto Trader Dashboard at http://localhost:8080")
    web.run_app(app, host="0.0.0.0", port=8080)
