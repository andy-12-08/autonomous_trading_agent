"""Central configuration: API keys from the environment, risk limits, and session times."""

import os
import pytz
from dotenv import load_dotenv

load_dotenv()

# US/Eastern — single source for session timing and bar end times
ET = pytz.timezone("America/New_York")

# Alpaca
ALPACA_KEY    = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
ALPACA_BASE_URL = os.getenv("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets")


# Account
ACCOUNT_SIZE = 25_000.0

# Margin account — intraday proceeds available immediately (no T+1 settlement wait).
# Daily buy cap and risk ceiling are recomputed each morning from live equity
# (see reset_daily_state) so they scale automatically as the account grows or shrinks.
MAX_DAILY_CAPITAL_PCT  = 0.80   # daily buy cap  = 80% of equity
MAX_RISK_PER_TRADE_HARD_PCT = 0.015  # hard risk ceiling = 1.5% of equity per trade

# Seed values — overwritten at session reset with live equity; used only as fallback
# if the broker API is unavailable at startup.
MAX_DAILY_CAPITAL  = round(ACCOUNT_SIZE * MAX_DAILY_CAPITAL_PCT, -2)   # $20,000

# Risk per trade
# Target 0.75% of equity (risk-size); hard ceiling = MAX_RISK_PER_TRADE_HARD_PCT of equity.
MAX_RISK_PER_TRADE_PCT = 0.0075
MAX_RISK_PER_TRADE     = round(ACCOUNT_SIZE * MAX_RISK_PER_TRADE_HARD_PCT, 2)  # $375

# Daily drawdown guard
DAILY_DRAWDOWN_LIMIT_PCT = 0.02                              # 2% of equity
DAILY_DRAWDOWN_LIMIT     = ACCOUNT_SIZE * DAILY_DRAWDOWN_LIMIT_PCT  # $500 seed

# Exposure cap
MAX_TOTAL_EXPOSURE_PCT = 0.80       # hard ceiling 80% of equity — margin gives the headroom
MIN_TOTAL_EXPOSURE_PCT = 0.30       # deploy at least 30% when conditions allow

# Position limits
MAX_CONCURRENT_POSITIONS = 8           # 8 simultaneous positions across 8 sector buckets
MAX_POSITION_SIZE        = MAX_DAILY_CAPITAL  # updated alongside MAX_DAILY_CAPITAL each morning
MIN_POSITION_SIZE        = 0.0         # no minimum position size
MAX_TRADES_PER_DAY       = 20          # PDT unlocked at $25k — quality gate does real filtering

# Conviction-weighted position sizing.
# Each entry is (min_signal_score, fraction_of_MAX_DAILY_CAPITAL).
# Tiers are evaluated in order; first match wins.
# Actual cap = min(intended, remaining daily capital) — never exceeds what's left.
# Max 15% of equity per trade keeps concentration risk manageable across 8 positions.
CONVICTION_TIERS = [
    (8.5, 0.18),   # high-conviction  (≥8.5): up to 18% of daily capital (~$3,600)
    (7.5, 0.14),   # strong           (≥7.5): up to 14%                   (~$2,800)
    (0.0, 0.09),   # below 7.5:               up to  9%                   (~$1,800)
]

# High-conviction threshold: allows a second position in the same sector bucket
# (does NOT override position sizing — all positions are still risk-sized)
HIGH_CONVICTION_THRESHOLD = 9

# Mid-session PnL degradation — reduce position sizes as intraday losses accumulate.
# Each entry: (daily_pnl_pct_threshold, size_multiplier).
# Tiers are evaluated in order; first match (most severe) wins.
# Hard stop at -2% is enforced separately by DAILY_DRAWDOWN_LIMIT.
INTRADAY_PNL_TIERS = [
    (-0.015, 0.40),   # -1.5%+ drawdown: size ×0.40 — severe, one bad trade from hard stop
    (-0.010, 0.70),   # -1.0%+ drawdown: size ×0.70 — early warning, dial back aggression
]


# Quality filters
MIN_REWARD_TO_RISK    = 2.0         # minimum 2:1 R:R — cut losses fast, let winners run
MIN_SIGNAL_CONFIDENCE = 6           # hard floor
MIN_VOL_RATIO_ENTRY   = 0.7         # require stock is on pace for ≥70% of avg daily volume (time-adjusted)
MAX_SPREAD_PCT        = 0.02        # max 2.0% bid-ask spread — IEX quotes are wider than NBBO; true NBBO for liquid stocks is ~0.01%

# Early-window vol_ratio relaxation
# In the first 55 minutes after open (9:35–10:30 ET), cumulative volume is still
# building and time-adjusted vol_ratio understates true activity.
# Option A: relax threshold for all stocks in the early window.
# Option B: relax further for confirmed gap-and-go setups (gap ≥ 2%, holding VWAP).
EARLY_WINDOW_END_HOUR    = 10       # early window ends at start of 10:30 ET
EARLY_WINDOW_END_MIN     = 30
EARLY_WINDOW_VOL_RATIO   = 0.6     # Option A: general early-window floor (was 0.7)
GAP_AND_GO_VOL_RATIO     = 0.5     # Option B: gap stocks floor (gap ≥ 2% + above VWAP)
GAP_AND_GO_MIN_VOL_PCT   = 2.0     # minimum gap % to qualify for Option B relaxation

# Stop / take-profit defaults (initial bracket order)
DEFAULT_STOP_LOSS_PCT   = 0.012  # initial stop: 1.2% below entry (or ATR-based if larger)
DEFAULT_TAKE_PROFIT_PCT = 0.025  # fallback for DB default only — not the active exit
ATR_STOP_MULTIPLIER     = 1.5   # initial stop placed at 1.5× ATR from entry

# Step-trailing stop parameters
# Phase 1 (breakeven): when price reaches entry + BREAKEVEN_TRIGGER_PCT,
#   move stop to entry × (1 − BREAKEVEN_STOP_BUFFER) and set trailing=True.
# Phase 2 (step-trail): each position-management tick, while
#   current_price ≥ current_stop × (1 + TRAIL_STEP_TRIGGER_PCT),
#   step the stop up by TRAIL_STEP_SIZE_PCT.  Loop catches large price jumps.
BREAKEVEN_TRIGGER_PCT  = 0.002  # +0.2% gain triggers the breakeven move
BREAKEVEN_STOP_BUFFER  = 0.001  # stop set to entry × (1 − 0.001); 0.1% below entry
TRAIL_STEP_TRIGGER_PCT = 0.003  # stop steps for every +0.3% above the current stop
TRAIL_STEP_SIZE_PCT    = 0.001  # each step raises the stop by 0.1%

# Safety TP for bracket-order validity — set far above entry so the Alpaca TP leg
# never fires intraday.  The step-trailing stop is the real exit mechanism.
BRACKET_TP_SAFETY = 3.0  # TP = entry × 3.0 (200% above entry — unreachable intraday)

# Confidence-scaled position sizing
# Higher conviction signals get proportionally larger size.
# Applied on top of the volatility regime factor.
# conf < 6 is blocked by MIN_SIGNAL_CONFIDENCE so only 6–10 are reachable.
CONFIDENCE_SIZE_SCALE: dict[int, float] = {
    10: 1.20,   # maximum conviction — 20% above normal size
    9:  1.00,   # strong — full normal size (baseline)
    8:  0.85,   # good — slightly reduced
    7:  0.70,   # solid — moderately smaller bet
    6:  0.55,   # minimum passing — materially smaller bet
}

# Sector buckets
# Max 1 position per bucket unless high-conviction (confidence ≥ 9)
SECTOR_BUCKETS = {
    "tech":        ["AAPL","MSFT","NVDA","AMD","GOOGL","META","INTC","QCOM","AVGO",
                    "ORCL","CRM","ADBE","NFLX","UBER","PLTR","CRWD","PANW","SNOW","DDOG","ARM",
                    "WDC","MU","STX","SNDK"],
    "consumer":    ["AMZN","TSLA","WMT","TGT","COST","NKE","DIS","MCD","HD","LOW",
                    "SBUX","ABNB","BKNG","F","GM"],
    "finance":     ["JPM","BAC","GS","WFC","MS","C","V","MA","AXP","BLK","SCHW",
                    "COF","USB"],
    "crypto":      ["COIN","SQ","IBIT","MSTU"],
    "energy":      ["XOM","CVX","COP","SLB","EOG","MPC","PSX","VLO","HAL","DVN"],
    "healthcare":  ["UNH","JNJ","PFE","ABBV","MRK","LLY","TMO","AMGN","BMY","CVS",
                    "GILD","ISRG","MRNA","REGN","VRTX"],
    "industrial":  ["BA","CAT","GE","HON","UPS","FDX","RTX","DE","LMT","MMM"],
    "index_etf":   ["SPY","QQQ","IWM","DIA","XLK","XLF","XLE","XLV","XLI","XLC",
                    "GLD","SLV","TLT","TQQQ","SOXL"],
}

# Flat symbol-to-bucket lookup (built from SECTOR_BUCKETS)
SYMBOL_BUCKET: dict[str, str] = {
    sym: bucket
    for bucket, symbols in SECTOR_BUCKETS.items()
    for sym in symbols
}

# Watchlist: ~75 liquid, large-cap stocks across all sectors — always scanned
WATCHLIST = [
    # tech (24)
    "AAPL","MSFT","NVDA","AMD","GOOGL","META","INTC","QCOM","AVGO",
    "ORCL","CRM","ADBE","NFLX","UBER","PLTR","CRWD","PANW","SNOW","DDOG","ARM",
    "WDC","MU","STX","SNDK",
    # consumer (10)
    "AMZN","TSLA","WMT","TGT","COST","NKE","DIS","MCD","HD","SBUX",
    # finance (10)
    "JPM","BAC","GS","WFC","MS","V","MA","AXP","BLK","SCHW",
    # energy (6)
    "XOM","CVX","COP","SLB","EOG","MPC",
    # healthcare (9)
    "UNH","JNJ","PFE","ABBV","MRK","LLY","AMGN","GILD","MRNA",
    # industrial (6)
    "BA","CAT","GE","HON","UPS","RTX",
    # index ETFs (9)
    "SPY","QQQ","IWM","XLK","XLF","XLE","XLV","GLD","IBIT",
]

# Morning study window
# 8:30 ET: pre-market study begins — catches 8:30 economic data (CPI, NFP, PCE, GDP)
#           and reads 4.5 hours of pre-market price action before the open
# 9:30 ET: market opens — study continues if not yet complete
# 9:35 ET: trading begins
MARKET_OPEN_HOUR        = 9
MARKET_OPEN_MIN         = 30   # exchange opens
STUDY_START_HOUR        = 8    # study begins at 8:30 ET (catches 8:30 macro data)
STUDY_START_MIN         = 30   # study begins at 8:30 ET
STUDY_END_HOUR          = 9    # study ends at 9:30 ET
STUDY_END_MIN           = 30   # trading begins at the open
MARKET_CLOSE_HOUR       = 15
MARKET_CLOSE_MIN        = 45   # last entry window closes at 3:45

# Prime entry window — highest-quality momentum occurs in the first 45 min after open.
# Outside this window, only very high conviction setups are allowed through.
PRIME_ENTRY_END_HOUR    = 10
PRIME_ENTRY_END_MIN     = 15
MIDDAY_ENTRY_MIN_SCORE  = 9.0  # signal score required outside prime window
MIDDAY_ENTRY_MIN_CONF   = 8    # signal confidence required outside prime window

# Scheduler fires every SCAN_INTERVAL_MINUTES throughout the day.
# During high-volume windows (9:35–11:00 and 2:30–3:45) every cycle runs a full scan.
# During midday, the full market scan is throttled to MIDDAY_SCAN_INTERVAL_MINUTES
# to reduce API calls during slow hours; position management still runs every 5 min.
SCAN_INTERVAL_MINUTES        = 10   # scheduler base cadence (every 10 min all day)
MIDDAY_SCAN_INTERVAL_MINUTES = 20   # full scan every 20 min during midday low-volume period

# Dynamic universe screener
# Each cycle: fetch top movers + most-actives from Alpaca, merge with WATCHLIST.
# Falls back gracefully to WATCHLIST if the screener API is unavailable.
UNIVERSE_MAX_SYMBOLS = 100      # 74 watchlist + 26 discovery; fits in 90s budget
SCREENER_MIN_PRICE   = 3.0      # filter out sub-$3 micro-cap garbage; spread + dollar-vol guard the rest
SCREENER_MAX_PRICE   = 500.0    # filter out very expensive illiquid names

# Screener slot allocation
# Fixed watchlist stocks are guaranteed every cycle — exclude them from screener
# results so all screener slots go to genuine discovery.
# Each source gets a protected quota so gainers always contributes fresh names
# even when snapshot and most-actives overlap heavily.
SCREENER_SNAPSHOT_SLOTS   = 15   # broad market sweep — top N non-watchlist stocks
SCREENER_ACTIVES_SLOTS    = 7    # real-time volume leaders not already found
SCREENER_GAINERS_SLOTS    = 4    # catalyst/% movers not already found (SNDK-type plays)

# GFV (good-faith violation) avoidance
# A GFV occurs when you buy with unsettled proceeds AND sell before those proceeds
# settle. We prevent this by flagging any position bought with same-day proceeds.
GFV_LOCK_DAYS = 1               # lock GFV-funded positions for 1 business day

# High-volume trading windows
# (start_hour, start_min, end_hour, end_min)  — all ET
HIGH_VOLUME_WINDOWS = [
    (9, 30, 11, 0),    # Morning momentum: open through first hour
    (14, 30, 15, 44),  # Afternoon power hour: into the close
]
# Signal score gates — risk management (stops + sizing) is the real protection,
# not artificially high thresholds that block legitimate midday setups.
MIDDAY_MIN_SIGNAL_SCORE = 6.0  # mean-reversion, VWAP reclaim, consolidation breaks
NORMAL_MIN_SIGNAL_SCORE = 6.0  # opening hour: gap-and-go, ORB, momentum

# Volatility regime sizing
# atr_pct = ATR / price.  The higher the volatility, the smaller the position.
VOL_REGIME_THRESHOLDS = [
    # (atr_pct_above, size_factor, label)
    (0.040, 0.35, "extreme"),   # ATR > 4%  → 35% of normal size
    (0.025, 0.55, "high"),      # ATR > 2.5% → 55%
    (0.015, 0.75, "elevated"),  # ATR > 1.5% → 75%
    (0.000, 1.00, "normal"),    # ATR ≤ 1.5% → full size
]
# If ATR/price > this, skip the trade entirely — too dangerous
MAX_TRADEABLE_ATR_PCT = 0.05   # 5% ATR/price is the absolute cap

# Signal quality gate
# Items below this score are dropped before the algo engine evaluates them
MIN_SIGNAL_SCORE_TO_AI = 5.0   # must match per-mode bars — 5.0–5.9 items add no value

# VIX regime-aware sizing
# SPY 10-day realized volatility (annualized %) is used as a market fear proxy.
# Applied as an additional multiplier on top of per-stock ATR sizing.
VIX_REGIME_THRESHOLDS: list[tuple[float, float, str]] = [
    # (realized_vol_pct_above, size_factor, label)
    (30.0, 0.40, "extreme"),   # vol > 30% → fear spike
    (20.0, 0.70, "elevated"),  # vol > 20% → elevated fear
    (13.0, 0.90, "normal"),    # vol > 13% → normal
    ( 0.0, 1.10, "calm"),      # vol ≤ 13% → calm, slight boost
]

# Per-symbol cooling off
# If a symbol's last N closed trades show win rate below the threshold,
# skip it until its win rate recovers naturally.
SYMBOL_COOLING_LOOKBACK     = 10    # min closed trades before cooling activates
SYMBOL_COOLING_MIN_WIN_RATE = 0.25  # cool if recent WR < 25%

# Confidence drift audit
# Flag in daily email if 7-day avg confidence deviates > N pts from 90-day avg.
CONFIDENCE_DRIFT_THRESHOLD = 2.0

# Consecutive-loss guard
MAX_CONSECUTIVE_LOSSES_NORMAL  = 2   # after 2 losses: raise confidence bar
MAX_CONSECUTIVE_LOSSES_STANDASIDE = 3  # after 3 losses: stand aside

# Portfolio heat
# Heat = sum of (entry - stop) × qty across ALL open positions.
# If every stop hits simultaneously, total loss must not exceed 2% of equity.
MAX_PORTFOLIO_HEAT_PCT = 0.02     # 2% of equity — same as daily drawdown limit

# Circuit breaker
CIRCUIT_BREAKER_SPY_DROP_PCT   = -1.5   # SPY down ≥ 1.5% from today's open → stand aside
CIRCUIT_BREAKER_UVXY_SURGE_PCT =  5.0   # UVXY up ≥ 5% intraday → stand aside

# Earnings blackout
EARNINGS_BLACKOUT_DAYS = 2        # skip stocks reporting within 2 calendar days

# Time stop
# After TIME_STOP_MINUTES the position must be up at least TIME_STOP_MIN_GAIN_PCT
# from entry, otherwise it is exited.  The old progress-toward-TP metric was broken
# because BRACKET_TP_SAFETY=3.0 makes the TP unreachable intraday.
TIME_STOP_MINUTES      = 60       # max time to wait for thesis to materialise
TIME_STOP_MIN_GAIN_PCT = 0.0      # exit if position is in the red (pnl < 0) at deadline

# Partial profit (scale-out)
PARTIAL_PROFIT_TRIGGER_PCT = 0.50  # sell 50% of shares when price hits 50% of TP range

# Correlation guard
MAX_HOLDING_CORRELATION    = 0.70  # block new position if 10-day return corr > this (tightened for 8-position portfolio)

# Gap-and-go setup
# First 90-min institutional play: gap from prior close + volume + holding above open
GAP_AND_GO_MIN_PCT      = 1.5   # minimum % gap from prior close to qualify
GAP_AND_GO_MAX_PCT      = 8.0   # above this the stock is too extended to chase
GAP_AND_GO_CUTOFF_HOUR  = 11    # no new gap entries at or after 11:00 AM ET
GAP_AND_GO_CUTOFF_MIN   = 0

# Dynamic confidence threshold
DYNAMIC_WINRATE_LOOKBACK   = 10    # last N closed trades to assess recent form
DYNAMIC_WINRATE_THRESHOLD  = 0.40  # if recent win rate < 40% → raise confidence bar by 1

# Daily email / SMTP
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "okaforandrew416@gmail.com")
SMTP_HOST       = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER       = os.getenv("SMTP_USER", "")
SMTP_PASS       = os.getenv("SMTP_PASS", "")

# Logging paths
DB_PATH  = "trading_log.db"
LOG_FILE = "bot.log"
