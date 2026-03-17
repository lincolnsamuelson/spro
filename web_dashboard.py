"""
Crypto Trader Competition Dashboard
Run: python web_dashboard.py
Open: http://localhost:8080
"""

import json
import time
import os
from pathlib import Path
from aiohttp import web

BASE_DIR = Path(__file__).parent
PORTFOLIO_FILE = BASE_DIR / "portfolio.json"
CONFIG_FILE = BASE_DIR / "config.json"
LEARNINGS_FILE = BASE_DIR / "learnings.json"

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

# Trader identities — must match portfolio_manager.py
TRADER_COLORS = {
    "blitz":    "#3b82f6",   # blue
    "phantom":  "#8b5cf6",   # purple
    "maverick": "#f97316",   # orange
    "viper":    "#22c55e",   # green
    "ghost":    "#ec4899",   # pink
}

TRADER_NAMES = {
    "blitz":    "BLITZ",
    "phantom":  "PHANTOM",
    "maverick": "MAVERICK",
    "viper":    "VIPER",
    "ghost":    "GHOST",
}


def load_portfolio():
    if not PORTFOLIO_FILE.exists():
        return {"traders": {}}
    with open(PORTFOLIO_FILE) as f:
        return json.load(f)


def load_learnings():
    if not LEARNINGS_FILE.exists():
        return {"blacklist": [], "probation": [], "rules_learned": []}
    try:
        with open(LEARNINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"blacklist": [], "probation": [], "rules_learned": []}


# Rolling equity history per trader (filled by API refresh)
_trader_equity: dict = {}  # trader_id -> list of {x: timestamp_ms, y: value}
MAX_EQUITY_POINTS = 360


def update_equity_points(portfolio_data):
    now_ms = time.time() * 1000
    starting = CONFIG["trading"]["starting_balance"]
    for tid, tdata in portfolio_data.get("traders", {}).items():
        if tid not in _trader_equity:
            _trader_equity[tid] = []
        cash = tdata.get("cash", 0)
        positions = tdata.get("positions", {})
        total = cash
        for sym, pos in positions.items():
            margin = pos.get("margin", 0)
            total += margin  # approximate (no live price in static file)
        _trader_equity[tid].append({"x": now_ms, "y": round(total, 2)})
        if len(_trader_equity[tid]) > MAX_EQUITY_POINTS:
            _trader_equity[tid] = _trader_equity[tid][-MAX_EQUITY_POINTS:]


async def api_data(request):
    portfolio = load_portfolio()
    learnings = load_learnings()
    starting = CONFIG["trading"]["starting_balance"]

    update_equity_points(portfolio)

    # Build scoreboard
    scoreboard = []
    total_pool = 0
    all_positions = []
    for tid, tdata in portfolio.get("traders", {}).items():
        cash = tdata.get("cash", starting)
        realized = tdata.get("realized_pnl", 0)
        wins = tdata.get("total_wins", 0)
        losses = tdata.get("total_losses", 0)
        streak = tdata.get("win_streak", 0)
        style = tdata.get("style", "")

        # Calc total value
        total = cash
        for sym, pos in tdata.get("positions", {}).items():
            margin = pos.get("margin", 0)
            total += margin
            all_positions.append({
                "symbol": sym,
                "trader_id": tid,
                "side": pos.get("side", "buy"),
                "leverage": pos.get("leverage", 1),
                "margin": round(margin, 2),
                "entry_price": pos.get("entry_price", 0),
            })

        pnl = total - starting
        pnl_pct = (pnl / starting * 100) if starting > 0 else 0
        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        total_pool += total

        generation = tdata.get("generation", 1)
        times_fired = tdata.get("times_fired", 0)
        # Build display name with generation
        base_name = TRADER_NAMES.get(tid, tid.upper())
        display_name = base_name if generation <= 1 else f"{base_name} {'I' * generation}"

        scoreboard.append({
            "trader_id": tid,
            "name": display_name,
            "style": style,
            "color": TRADER_COLORS.get(tid, "#64748b"),
            "equity": round(total, 2),
            "cash": round(cash, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "realized_pnl": round(realized, 2),
            "positions": len(tdata.get("positions", {})),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "win_streak": streak,
            "generation": generation,
            "times_fired": times_fired,
        })

    scoreboard.sort(key=lambda x: x["equity"], reverse=True)
    for i, b in enumerate(scoreboard):
        b["rank"] = i + 1

    # Chart datasets
    datasets = []
    for tid in _trader_equity:
        datasets.append({
            "trader_id": tid,
            "name": TRADER_NAMES.get(tid, tid.upper()),
            "color": TRADER_COLORS.get(tid, "#64748b"),
            "points": _trader_equity[tid],
        })

    return web.json_response({
        "scoreboard": scoreboard,
        "chart": {"datasets": datasets, "starting_cash": starting},
        "total_pool": round(total_pool, 2),
        "positions": all_positions,
        "learnings": {
            "blacklist": learnings.get("blacklist", []),
            "probation": learnings.get("probation", []),
            "rules_count": len(learnings.get("rules_learned", [])),
        },
        "config": {
            "mode": CONFIG["trading"]["mode"],
            "default_leverage": CONFIG["trading"].get("default_leverage", 15),
            "max_leverage": CONFIG["trading"].get("max_leverage", 25),
            "stop_loss_pct": round(CONFIG["risk"].get("default_stop_loss_pct", 0.015) * 100, 1),
            "trailing_stop_pct": round(CONFIG["risk"].get("trailing_stop_pct", 0.01) * 100, 1),
        },
    })


async def index(request):
    return web.Response(text=HTML, content_type="text/html")


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trader Competition</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root {
    --bg: #0a0e17;
    --card: #111827;
    --border: #1e293b;
    --text: #e2e8f0;
    --text-dim: #64748b;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #eab308;
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
  .header h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
  .header h1 span { color: #f97316; }
  .header-meta {
    display: flex; gap: 16px; align-items: center;
    font-size: 12px; color: var(--text-dim);
  }
  .live-dot {
    width: 8px; height: 8px; background: var(--green);
    border-radius: 50%; display: inline-block;
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.3;} }
  .mode-badge {
    background: #1e3a5f; color: #3b82f6;
    padding: 4px 10px; border-radius: 4px;
    font-weight: 600; font-size: 11px;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .container { padding: 24px 32px; max-width: 1600px; margin: 0 auto; }

  /* Pool KPI */
  .pool-bar {
    display: flex; gap: 24px; margin-bottom: 24px;
    align-items: center; font-size: 14px;
  }
  .pool-bar .val { font-size: 28px; font-weight: 700; }
  .pool-bar .label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; }

  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; overflow: hidden; margin-bottom: 24px;
  }
  .card-header {
    padding: 16px 20px; border-bottom: 1px solid var(--border);
    font-size: 14px; font-weight: 600;
    display: flex; justify-content: space-between; align-items: center;
  }
  .card-body { padding: 16px 20px; }

  /* Chart */
  .chart-container { padding: 20px; height: 380px; position: relative; }
  canvas { width: 100% !important; height: 100% !important; }

  /* Scoreboard table */
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left; font-size: 10px; text-transform: uppercase;
    letter-spacing: 1px; color: var(--text-dim);
    padding: 10px 14px; border-bottom: 1px solid var(--border);
  }
  td { padding: 12px 14px; font-size: 13px; border-bottom: 1px solid #1a2332; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1a2332; }

  .rank-1 { font-size: 18px; }
  .rank-medal { font-weight: 700; font-size: 16px; }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--text-dim); }

  .trader-color {
    width: 12px; height: 12px; border-radius: 3px;
    display: inline-block; vertical-align: middle; margin-right: 8px;
  }

  .equity-big { font-size: 20px; font-weight: 700; }

  /* Positions mini */
  .pos-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 8px; padding: 16px 20px;
  }
  .pos-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 12px; background: #0d1420; border-radius: 8px; font-size: 12px;
  }

  @media (max-width: 900px) {
    .container { padding: 16px; }
    .pool-bar { flex-direction: column; gap: 8px; }
  }
</style>
</head>
<body>

<div class="header">
  <h1><span>TRADER</span> COMPETITION</h1>
  <div class="header-meta">
    <span class="live-dot"></span>
    <span id="last-updated">Loading...</span>
    <span class="mode-badge" id="mode-badge">PAPER</span>
  </div>
</div>

<div class="container">
  <!-- Pool summary -->
  <div class="pool-bar">
    <div>
      <div class="label">Total Pool</div>
      <div class="val" id="pool-total">--</div>
    </div>
    <div>
      <div class="label">Per Trader Start</div>
      <div class="val" id="pool-start">$50.00</div>
    </div>
    <div>
      <div class="label">Positions</div>
      <div class="val" id="pool-positions">--</div>
    </div>
    <div>
      <div class="label">Leverage</div>
      <div class="val" id="pool-leverage">--</div>
    </div>
  </div>

  <!-- Competition Chart -->
  <div class="card">
    <div class="card-header">
      COMPETITION — EQUITY OVER TIME
      <span style="font-size:12px;color:var(--text-dim);" id="chart-info">--</span>
    </div>
    <div class="chart-container">
      <canvas id="comp-chart"></canvas>
    </div>
  </div>

  <!-- Scoreboard -->
  <div class="card">
    <div class="card-header">SCOREBOARD</div>
    <div class="card-body" style="padding:0;">
      <table>
        <thead><tr>
          <th>Rank</th><th>Trader</th><th>Style</th>
          <th>Equity</th><th>P&L</th><th>P&L %</th>
          <th>Positions</th><th>W / L</th><th>Win Rate</th><th>Streak</th><th>Fired</th>
        </tr></thead>
        <tbody id="scoreboard-body"></tbody>
      </table>
    </div>
  </div>

  <!-- Open Positions -->
  <div class="card">
    <div class="card-header">
      Open Positions
      <span style="font-size:12px;color:var(--text-dim);" id="pos-count">--</span>
    </div>
    <div class="pos-grid" id="pos-grid"></div>
  </div>
</div>

<script>
function fmt(n, d=2) {
  if (n===null||n===undefined) return '--';
  return n.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
}
function pnlClass(v) { return v>0?'positive':v<0?'negative':'neutral'; }

let compChart = null;

function renderChart(chartData) {
  const ctx = document.getElementById('comp-chart');
  if (!ctx) return;

  const datasets = chartData.datasets.map(ds => ({
    label: ds.name || ds.trader_id,
    data: ds.points,
    borderColor: ds.color,
    borderWidth: 2.5,
    backgroundColor: 'transparent',
    pointRadius: 0,
    pointHitRadius: 10,
    tension: 0.2,
  }));

  if (compChart) {
    compChart.data.datasets = datasets;
    compChart.update('none');
    return;
  }

  compChart = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          labels: {
            color: '#e2e8f0',
            font: { family: "'SF Mono', monospace", size: 11 },
            usePointStyle: true,
            pointStyle: 'rectRounded',
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
          titleFont: { family: "'SF Mono', monospace", size: 11 },
          callbacks: {
            title: function(items) {
              if (!items.length) return '';
              return new Date(items[0].parsed.x).toLocaleTimeString();
            },
            label: function(item) {
              return item.dataset.label + ':  $' + item.parsed.y.toFixed(2);
            },
          },
        },
      },
      scales: {
        x: {
          type: 'time',
          time: {
            tooltipFormat: 'HH:mm:ss',
            displayFormats: { second:'HH:mm:ss', minute:'HH:mm', hour:'HH:mm' },
          },
          grid: { color: '#1e293b22', drawBorder: false },
          ticks: { color: '#64748b', font: { family: "'SF Mono', monospace", size: 10 }, maxTicksLimit: 10 },
        },
        y: {
          grid: { color: '#1e293b44', drawBorder: false },
          ticks: {
            color: '#64748b',
            font: { family: "'SF Mono', monospace", size: 10 },
            callback: function(v) { return '$' + v.toFixed(2); },
          },
        },
      },
    },
  });
}

function renderScoreboard(scoreboard) {
  const body = document.getElementById('scoreboard-body');
  const medals = {1: '1st', 2: '2nd', 3: '3rd'};

  body.innerHTML = scoreboard.map(b => {
    const rankLabel = medals[b.rank] || b.rank + 'th';
    const pnlSign = b.pnl >= 0 ? '+' : '';
    const streakStr = b.win_streak > 0 ? 'x' + b.win_streak : '---';
    return `
      <tr>
        <td class="rank-medal">${rankLabel}</td>
        <td>
          <span class="trader-color" style="background:${b.color};"></span>
          <strong>${b.name}</strong>
        </td>
        <td style="color:var(--text-dim);">${b.style}</td>
        <td class="equity-big">$${fmt(b.equity)}</td>
        <td class="${pnlClass(b.pnl)}">${pnlSign}$${fmt(Math.abs(b.pnl))}</td>
        <td class="${pnlClass(b.pnl_pct)}">${pnlSign}${fmt(Math.abs(b.pnl_pct),1)}%</td>
        <td>${b.positions}</td>
        <td>${b.wins} / ${b.losses}</td>
        <td>${fmt(b.win_rate,1)}%</td>
        <td>${streakStr}</td>
        <td style="color:${b.times_fired > 0 ? 'var(--red)' : 'var(--text-dim)'};">${b.times_fired > 0 ? b.times_fired + 'x' : '---'}</td>
      </tr>
    `;
  }).join('');
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
  // Pool summary
  document.getElementById('pool-total').textContent = '$' + fmt(d.total_pool);
  document.getElementById('pool-start').textContent = '$' + fmt(d.chart.starting_cash);
  document.getElementById('pool-positions').textContent = d.positions.length;
  document.getElementById('pool-leverage').textContent = d.config.default_leverage + 'x — ' + d.config.max_leverage + 'x';

  // Chart
  if (d.chart.datasets.length > 0 && d.chart.datasets.some(ds => ds.points.length > 1)) {
    renderChart(d.chart);
    const pts = d.chart.datasets.reduce((sum, ds) => sum + ds.points.length, 0);
    document.getElementById('chart-info').textContent = pts + ' data points';
  }

  // Scoreboard
  renderScoreboard(d.scoreboard);

  // Positions
  const posGrid = document.getElementById('pos-grid');
  document.getElementById('pos-count').textContent = d.positions.length + ' open';
  if (d.positions.length === 0) {
    posGrid.innerHTML = '<div style="padding:20px;color:var(--text-dim);text-align:center;">No positions yet — deploying capital...</div>';
  } else {
    posGrid.innerHTML = d.positions.map(p => {
      const trader = d.scoreboard.find(s => s.trader_id === p.trader_id);
      const color = trader?.color || '#64748b';
      const tname = trader?.name || p.trader_id;
      return `
        <div class="pos-item">
          <span>
            <span class="trader-color" style="background:${color};"></span>
            <strong>${p.symbol.split('-')[0].toUpperCase()}</strong>
            <span style="color:var(--yellow);margin-left:6px;">${p.leverage}x</span>
          </span>
          <span>
            <span style="color:var(--text-dim);">M:</span>$${fmt(p.margin)}
            <span style="color:${color};margin-left:8px;font-weight:600;">${tname}</span>
          </span>
        </div>
      `;
    }).join('');
  }

  // Meta
  document.getElementById('mode-badge').textContent = d.config.mode.toUpperCase();
  document.getElementById('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/api/data", api_data)

if __name__ == "__main__":
    print("Starting Trader Competition Dashboard at http://localhost:8080")
    web.run_app(app, host="0.0.0.0", port=8080)
