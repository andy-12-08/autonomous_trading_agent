# Autonomous Stock Trading Bot

An AI-powered day-trading bot that runs on Alpaca's paper-trading API. It uses Claude (Anthropic) to make buy/sell decisions on a curated watchlist of US equities, enforcing strict risk rules, expectancy tracking, and daily self-review.

---

## What It Does

Every trading day the bot runs two independent loops:

**Every 2 minutes — Position Management**
- Checks open positions for time-based exits (max hold reached)
- Takes partial profits at +2R
- At 15:45 ET forces close of all positions (no overnight holds)
- Emails a daily P&L summary at end of day

**Every 10 minutes — Scan & Trade**
- 9:15–9:34 ET: runs a **Morning Study** — pulls market context, economic calendar, gap scan, breadth, trading history, and asks Claude to produce a structured Daily Trading Plan
- 9:35–15:44 ET: scans the watchlist, scores every setup with a programmatic signal scorer, enriches survivors with options flow / dark pool / insider data, then sends a curated shortlist to Claude for final buy/sell decisions
- Executes approved orders as bracket orders (entry + stop-loss + take-profit legs)

**Every Sunday 8 AM — Backtest**
- Simulates the signal-scoring strategy over 180 days of history
- Emails a report with win rate, expectancy, and recommended score-threshold adjustments

---

## Prerequisites

- Python 3.11+
- [Alpaca paper-trading account](https://alpaca.markets) — free, no real money
- [Anthropic API key](https://console.anthropic.com)
- Optional: SMTP credentials for email alerts

---

## Setup

**1. Clone and install dependencies**

```bash
git clone <repo-url>
cd automated_stocks_bot
pip install -r requirements.txt
```

**2. Create your `.env` file**

```bash
cp .env.example .env   # then fill in your keys
```

```env
ALPACA_KEY=your_alpaca_key
ALPACA_SECRET=your_alpaca_secret
ALPACA_ENDPOINT=https://paper-api.alpaca.markets

ANTHROPIC_API_KEY=your_anthropic_key

# Optional — email alerts
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your_app_password
ALERT_EMAIL=you@gmail.com
```

**3. Review `config.py`**

Key settings you may want to adjust before first run:

| Setting | Default | Meaning |
|---|---|---|
| `ACCOUNT_SIZE` | $10,000 | Target account size |
| `MAX_DAILY_CAPITAL` | $4,000 | Max capital deployed per day |
| `MAX_RISK_PER_TRADE` | $100 | Hard ceiling on loss per trade |
| `MAX_CONCURRENT_POSITIONS` | 3 | Max open positions at once |
| `WATCHLIST` | 40+ symbols | Universe of stocks to scan |

---

## Running the Bot

**Dry-run mode** (recommended for first run — no real orders, full logs):

```bash
python main.py --dry-run
```

**Live paper-trading mode:**

```bash
python main.py
```

The bot runs continuously until you stop it with `Ctrl+C`. It self-manages the schedule — you do not need to restart it each day.

**Run the backtester manually at any time:**

```bash
python -c "
from bootstrap import build_trading_stack
_, backtester = build_trading_stack()
backtester.run_backtest()
"
```

**View the trade review log:**

```bash
python -m utils.review_log
```

---

## Daily Schedule (ET)

| Time | What happens |
|---|---|
| 9:15 AM | Morning Study begins — Claude reads market + history, produces Daily Trading Plan |
| 9:35 AM | Trading opens — scan + trade loop starts |
| Every 2 min | Position management: stops, partial profits |
| Every 10 min | Universe scan + Claude trade decisions |
| 3:45 PM | All positions force-closed, daily summary emailed |
| Sunday 8 AM | Weekly backtest — 180-day strategy review emailed |

---

## Project Structure

```
automated_stocks_bot/
│
├── main.py              # Entry point — starts scheduler
├── bootstrap.py         # Wires all components together
├── config.py            # All settings and watchlist
│
├── prompts/
│   ├── trading_agent.md    # Claude system prompt for trade decisions
│   └── morning_study.md    # Claude system prompt for morning analysis
│
├── agents/
│   ├── agent.py            # TradingAgent — calls Claude for BUY/SELL decisions
│   ├── analyst.py          # MarketAnalyst — morning study orchestration
│   ├── study_data.py       # Data-collection helpers (market context, gaps, history)
│   └── dynamic_watchlist.py# Persists pre-Claude survivors across sessions
│
├── analysis/
│   ├── indicators.py       # Technical indicator computation (EMA, MACD, VWAP, RSI…)
│   ├── patterns.py         # Pattern detection (FVG, liquidity sweeps, volume profile)
│   ├── signal_scorer.py    # Programmatic setup scoring (0–10 scale)
│   ├── signal_rules.py     # Per-setup scoring rules (gap-and-go, VWAP reclaim, mean reversion)
│   ├── screener.py         # Dynamic universe builder (movers + liquid stocks)
│   ├── market_guard.py     # Circuit breaker, VIX regime, intraday regime detection
│   └── earnings.py         # Earnings blackout + correlation guards
│
├── core/
│   ├── broker.py           # AlpacaBroker — account, positions, market status
│   ├── broker_orders.py    # Order placement (market, bracket, stop updates)
│   ├── broker_data.py      # Market data (bars, quotes, snapshots, news)
│   └── database.py         # SQLite journal + module-level logger
│
├── data/
│   ├── options_flow.py     # Unusual options activity (via yfinance)
│   ├── dark_pool.py        # Dark pool print data
│   ├── insider_flow.py     # Insider transaction data
│   ├── pre_market.py       # Pre-market high/low levels
│   ├── yield_curve.py      # Macro yield curve signal
│   ├── short_interest.py   # Short interest / squeeze candidates
│   └── edgar.py            # SEC 8-K filing gate
│
├── risk/
│   ├── manager.py          # Position sizing, stop/TP computation, volatility filter
│   ├── gfv_tracker.py      # Good-faith violation tracking (cash account rule)
│   ├── bucket_manager.py   # Sector diversification limits
│   └── expectancy.py       # Rolling P&L stats, Kelly factor, revenge-trade guard
│
├── trading/
│   ├── orchestrator.py     # Scheduler shell — daily state, EOD, wires all mixins
│   ├── scanner.py          # build_watchlist_data — indicator + enrichment sweep
│   ├── positions.py        # Position snapshot, time stops, partial profits, position mgmt loop
│   ├── executor.py         # execute_decisions — risk checks + order placement
│   ├── trade_cycle.py      # _scan_body — full 10-min scan-and-trade cycle
│   ├── session_overrides.py# Morning-study threshold adjustments
│   └── notifier.py         # Trade alerts and daily summary emails
│
└── utils/
    ├── backtester.py       # 180-day historical simulation
    └── review_log.py       # CLI tool to print trade history and expectancy
```

---

## How the AI Decides

Claude never sees raw price data. It receives a structured JSON payload containing:

- Current open positions and account equity
- Scored watchlist candidates (only setups that passed the programmatic signal filter)
- The morning's Daily Trading Plan (bias, risk posture, macro flag)
- Enrichment signals: options flow, dark pool prints, insider activity, news headlines

Claude responds with a structured JSON array of decisions. The bot then runs each decision through a final gauntlet of hard rules before placing any order:

1. Earnings blackout window? → Skip
2. Revenge-trade guard triggered? → Skip
3. Dynamic confidence bar (recent win rate < 40%)? → Skip
4. Sector already at max exposure? → Skip
5. Correlation with existing position > threshold? → Skip
6. GFV (good-faith violation) risk? → Skip
7. Risk manager recomputes stop/TP from ATR — overrides Claude's levels

Only trades that pass all seven gates are sent to the broker.

---

## Output

- **Console + `bot.log`** — real-time structured logs for every decision
- **`trading_log.db`** — SQLite database with all decisions, outcomes, and daily summaries
- **Email alerts** — trade confirmations and end-of-day P&L summary (requires SMTP config)
