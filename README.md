<div align="center">

<img src="assets/logo-btcbot.png" alt="btcbot logo" width="512">

# btcbot

Latency-arbitrage trading bot for [Polymarket](https://polymarket.com)'s 5-minute BTC binary markets, with a real-time web dashboard.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)

[Overview](#overview) · [Quick Start](#quick-start) · [Dashboard](#dashboard) · [Configuration](#configuration) · [Architecture](#architecture)

</div>

---

**The edge**: BTC prices on Binance update in ~100ms. Polymarket odds and the Chainlink oracle lag behind. The bot detects these divergences and bets on the correct direction before the market catches up.

Inspired by **stargate5** — [$168K profit from 16,816 trades at 61.5% win rate](#about-stargate5) on these exact markets.

> [!CAUTION]
> This bot is for **educational and research purposes**. Trading on prediction markets involves real financial risk. Treat any target win rate as something to validate with fresh paper data, not as a guarantee. Always start with paper trading.

## Overview

Polymarket offers rolling 5-minute windows where you bet whether BTC will finish **higher** ("Up") or **lower** ("Down") than its starting price. Each outcome trades as a token priced $0.01–$0.99, resolving to $1.00 if correct or $0.00 if wrong via [Chainlink](https://data.chain.link/streams/btc-usd).

```
Binance WS ---- BTC ticks ----+
(sub-second)                   |
                         +-----------+
Gamma API -- markets --> |  Engine   |-- orders --> Polymarket CLOB
(every 30s)              |           |
                         |  signal   |
Polymarket WS -- odds -> |  -> risk  |
(real-time)              |  -> trade |
                         +-----+-----+
                               |
                         +-----v-----+
                         |  SQLite   | <-- Dashboard reads
                         +-----------+
```

The bot runs concurrent async tasks for Binance, Chainlink, and Polymarket feeds, plus market discovery, trading, risk monitoring, and unresolved-market sweeping, all coordinated via `asyncio.TaskGroup`. A **regime detector** continuously adapts the strategy based on whether the market is trending or choppy.

### Features

- **Real-time feeds** — Binance BTC/USDT WebSocket (~100ms) + Polymarket CLOB odds stream
- **Signal engine** — Sigmoid probability model comparing BTC momentum vs market odds, with regime-aware confidence scaling
- **Regime detection** — Tracks intra-window reversal rate to adapt strategy between trending and mean-reverting markets
- **Risk management** — Quarter-Kelly sizing, daily stop-loss, consecutive loss limits, and single-shot hedging with dynamic thresholds
- **Live dashboard** — Dark-themed UI with auto-refreshing panels, trade log, and Chart.js equity curves
- **Paper trading** — Full simulation with realistic spread modeling, same DB schema as live
- **CLI tools** — `serve`, `run`, `status`, `history`, `initdb`
- **JSON API** — `/api/live`, `/api/trades`, `/api/daily-pnl`, `/api/stats` for external integrations

## Quick Start

```bash
git clone https://github.com/sebbourgeois/polybtcbot.git
cd polybtcbot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
btcbot initdb
btcbot serve --paper -v
```

Open [http://localhost:8500](http://localhost:8500) — the dashboard shows live BTC prices, market odds, signals, and P&L in real-time.

> [!NOTE]
> Paper mode (default) simulates trades using real market data. No wallet or API keys needed.

## How the Strategy Works

### Per-Window Lifecycle (5 minutes)

| Phase | Window | Action |
|---|---|---|
| Warmup | 0s → 45s | Collect BTC price baseline. No trades. |
| Trading | 45s → 180s | Evaluate signals on every tick. Enter only on stronger confirmed edges. |
| Cooldown | 180s → 300s | No new entries. Monitor open positions and optional hedge trigger. |
| Resolution | 300s | Chainlink resolves. Record outcome, update P&L. |

### Signal Generation

On every BTC price tick, the signal engine:

1. Computes the **price delta** from the window's start price (via Chainlink, matching the oracle)
2. Measures **momentum** over 5s, 15s, and 30s windows
3. Estimates a **fair probability** of "Up" resolving via a sigmoid model, normalized by expected 5-minute BTC volatility
4. Compares the fair probability against **Polymarket's implied odds**
5. Fires a trade signal when the configured edge and strength thresholds are met

The fair probability is clamped dynamically based on the current **regime choppiness** score — tighter in mean-reverting markets (max 65%) to prevent overconfident bets that reverse.

### Regime Detection

The bot tracks a rolling window of the last 20 markets and measures how often the first-half momentum reversed by close. This produces a **choppiness score** (0.0 = trending, 1.0 = always reversing) that adapts 4 parameters in real-time:

| Parameter | Trending (0.0) | Choppy (1.0) |
|---|---|---|
| Fair prob clamp | 0.80 | 0.65 |
| Warmup period | 45s | 60s |
| Position size | 100% Kelly | 40% Kelly |
| Hedge threshold | configured % | configured + 5% |

### Risk Controls

| Control | Default | What it does |
|---|---|---|
| Position sizing | Quarter-Kelly | Sizes bets based on edge magnitude, scaled by regime |
| Max per trade | $25 | Caps any single bet by default |
| Daily stop-loss | $50 | Halts trading for the day |
| Consecutive losses | 5 | Pauses after 5 straight losses |
| Price cap | $0.65 | Never pays more than the configured token cap |
| Hedge trigger | 15% drop | Triggers at a percentage drop from entry price, once per market |
| Hedge time guard | Last 150s | Only hedges in the second half of the market window |
| Hedge price guard | opposite < $0.85 | Skips hedge if the opposite token is too expensive |

### How Hedging Works

If you buy "Up" at $0.55 and BTC reverses:

- **Without hedge**: Market resolves "Down" → you lose $0.55 per token (100%)
- **With hedge**: Buy "Down" once after a confirmed drawdown in the second half of the window → holds both sides → loss is capped to the spread between entry and hedge prices

## Dashboard

Start the dashboard with:

```bash
btcbot serve --paper -v
```

![btcbot dashboard](assets/polybtcbot.png)

| Page | URL | Content |
|---|---|---|
| Dashboard | `/` | Live BTC price, active market with progress bar, signal state, open position, P&L stats, recent trades, daily P&L chart |
| Trades | `/trades` | Full trade log with win/loss/hedge badges and execution details |
| History | `/history` | Daily P&L breakdown, cumulative equity curve, win rate stats |
| Stats | `/stats` | Day/week/month/all-time P&L rollups with equity curve, per-bucket P&L bars, and win/loss/hedged distribution |

The dashboard auto-refreshes via [htmx](https://htmx.org) — status panels update every 2 seconds, the trade table every 5 seconds. The Stats page uses a period toggle that redraws Chart.js instances in place without a full reload.

**JSON API** for programmatic access:

```bash
curl http://localhost:8500/api/live                  # Engine state, BTC price, signal, position
curl http://localhost:8500/api/trades                # Recent trades
curl http://localhost:8500/api/daily-pnl             # Daily P&L (for charting)
curl http://localhost:8500/api/stats?period=week     # Tiles, equity curve, bars, distribution for a period
```

## Configuration

All settings are env vars with a `BOT_` prefix. Copy the template:

```bash
cp .env.example .env
```

<details>
<summary><strong>Full configuration reference</strong></summary>

### Mode

| Variable | Default | Description |
|---|---|---|
| `BOT_PAPER_MODE` | `true` | Paper (simulated) or live trading |
| `BOT_LOG_LEVEL` | `INFO` | Logging level |

### Trading

| Variable | Default | Description |
|---|---|---|
| `BOT_BANKROLL` | `100.0` | Total trading capital (USD) |
| `BOT_MIN_SIGNAL_STRENGTH` | `0.30` | Minimum signal confidence (0–1) |
| `BOT_MIN_EDGE` | `0.05` | Minimum edge to enter (5%) |
| `BOT_BTC_5M_VOLATILITY` | `30.0` | Expected 5-min BTC volatility ($) |
| `BOT_LIMIT_SLIPPAGE` | `0.02` | Slippage tolerance for limit orders |

### Risk

| Variable | Default | Description |
|---|---|---|
| `BOT_MAX_POSITION_USD` | `25.0` | Max bet per trade |
| `BOT_MIN_POSITION_USD` | `2.0` | Min bet per trade |
| `BOT_MAX_DAILY_LOSS_USD` | `50.0` | Daily stop-loss |
| `BOT_MAX_CONSECUTIVE_LOSSES` | `5` | Pause after N straight losses |
| `BOT_MAX_PRICE_TO_PAY` | `0.65` | Max token price to buy |
| `BOT_HEDGE_TRIGGER` | `0.15` | Hedge when the held token drops 15% from entry price |

### Regime Detection

| Variable | Default | Description |
|---|---|---|
| `BOT_REGIME_WINDOW` | `20` | Number of recent markets to track for choppiness |

### Timing

| Variable | Default | Description |
|---|---|---|
| `BOT_DISCOVERY_INTERVAL_SEC` | `30.0` | Market discovery poll interval |
| `BOT_WARMUP_SEC` | `30.0` | No-trade warmup period |
| `BOT_COOLDOWN_SEC` | `60.0` | No-entry cooldown before resolution |
| `BOT_RISK_CHECK_SEC` | `2.0` | Hedge-check interval |

### Current paper profile

The checked-in `.env` is intentionally stricter than the library defaults while validating the strategy in paper mode:

- `BOT_MIN_SIGNAL_STRENGTH=0.65`
- `BOT_MIN_EDGE=0.12`
- `BOT_MAX_PRICE_TO_PAY=0.45`
- `BOT_HEDGE_TRIGGER=0.22`
- `BOT_WARMUP_SEC=45`
- `BOT_COOLDOWN_SEC=120`

This profile is designed to reduce low-quality entries and over-hedging while measuring whether the signal can sustain a realistic directional edge.

### Web

| Variable | Default | Description |
|---|---|---|
| `BOT_HOST` | `0.0.0.0` | Dashboard bind address |
| `BOT_PORT` | `8500` | Dashboard port |

### Auth (live trading only)

| Variable | Default | Description |
|---|---|---|
| `BOT_PRIVATE_KEY` | *(empty)* | Polygon wallet private key |

### API Endpoints

| Variable | Default |
|---|---|
| `BOT_CLOB_API` | `https://clob.polymarket.com` |
| `BOT_GAMMA_API` | `https://gamma-api.polymarket.com` |
| `BOT_CLOB_WS` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| `BOT_BINANCE_WS` | `wss://stream.binance.com:9443/ws/btcusdt@trade` |

</details>

## CLI Reference

```bash
btcbot serve [--paper|--live] [-v] [--port PORT]   # Bot + dashboard (recommended)
btcbot run [--paper|--live] [-v]                     # Bot headless (no UI)
btcbot status                                        # P&L summary + recent trades
btcbot history [--days 30]                           # Daily performance table
btcbot initdb                                        # Create SQLite database
```

> [!IMPORTANT]
> For live trading, see [SETUP.md](SETUP.md) for wallet setup, USDC funding, and contract approvals. Set `BOT_PAPER_MODE=false` and provide `BOT_PRIVATE_KEY` with a funded Polygon wallet.

## Architecture

```
btcbot/
├── config.py              # 29 settings via BOT_ env vars
├── cli.py                 # Typer CLI: serve, run, status, history, initdb
├── models.py              # Market, Signal, TradeRecord, OpenPosition
├── engine.py              # 5 concurrent async tasks (TaskGroup)
├── signal.py              # Sigmoid probability model with regime-aware clamping
├── risk.py                # Kelly sizing + safety limits + dynamic hedge threshold
├── regime.py              # Regime detector — trending vs mean-reverting classification
├── execution.py           # py-clob-client order placement
├── paper.py               # Simulated executor
├── market_discovery.py    # Gamma API polling
├── feeds/
│   ├── binance_ws.py      # BTC/USDT trade stream
│   └── polymarket_ws.py   # CLOB price WebSocket
├── storage/
│   ├── schema.sql         # 7 SQLite tables (WAL mode)
│   ├── db.py              # Async connection helper
│   └── repo.py            # Typed query functions
└── web/
    ├── app.py             # FastAPI app with engine lifespan
    ├── routes.py          # 6 HTML + 5 JSON endpoints
    ├── static/app.css     # Dark theme
    └── templates/         # Jinja2 + htmx auto-refresh
```

### Engine Tasks

| Task | Frequency | Role |
|---|---|---|
| `binance_ws` | Continuous | Stream BTC price ticks |
| `polymarket_ws` | Continuous | Stream market odds |
| `discovery` | Every 30s | Find active 5-minute market |
| `trading` | On price event | Evaluate signal, place trade |
| `risk` | Every 2s | Check hedge conditions for the active position |
| `sweep` | Every 5m | Resolve ended markets that were missed in the main flow |

### Database

SQLite with WAL mode. All trades, market outcomes, daily P&L, and price samples are persisted.

| Table | Content |
|---|---|
| `markets` | Discovered 5-minute windows and resolution outcomes |
| `trades` | Every entry and hedge trade |
| `market_results` | Per-market P&L with win/loss or hedged classification |
| `daily_pnl` | Aggregated daily statistics |
| `btc_prices` | BTC price samples (every 5s) |
| `poly_prices` | Polymarket token price snapshots |

## Testing

```bash
pytest tests/ -v
```

56 tests covering signal generation, hedge-aware accounting, risk management, the stats-page aggregation helpers (period bounds, bucket zero-fill, ISO-week rollup), and the `/api/stats` JSON endpoint, including warmup/cooldown handling, probability clamping, one-shot hedging, and restart-safe result math.

## About stargate5

This bot is based on analysis of the real [stargate5](https://polymarket.com/@stargate5) wallet:

| Metric | Value |
|---|---|
| Total P&L | $168,047 |
| Realized P&L | $235,764 |
| Unrealized P&L | -$67,717 |
| Predictions | 16,816 |
| Win Rate | 61.5% |
| Volume | $28,020,124 |

**What they do**: 75.6% directional bets + 24.4% merge/hedge operations. Exclusively trades 5-minute BTC binary markets with small positions ($15–60). Runs 24/7 as an automated bot.

**What works**: A 61.5% win rate on near-even odds, compounded over thousands of trades. The key is consistency and volume, not any single big win.

> [!WARNING]
> The -$67K in unrealized losses and 38.5% trade loss rate show real downside risk. Past performance does not guarantee future results.
