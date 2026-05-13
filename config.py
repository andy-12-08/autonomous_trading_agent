"""
Central configuration for the options trading bot.

All constants are defined here as the single source of truth.
Environment variables are loaded from .env for secrets.
"""

import os
import pytz
from dotenv import load_dotenv

load_dotenv()

# ── Timezone ─────────────────────────────────────────────────────────────────
ET = pytz.timezone("America/New_York")

# ── Alpaca credentials ────────────────────────────────────────────────────────
ALPACA_KEY      = os.getenv("ALPACA_KEY")
ALPACA_SECRET   = os.getenv("ALPACA_SECRET")
ALPACA_BASE_URL = os.getenv("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets")

# ── Account ───────────────────────────────────────────────────────────────────
ACCOUNT_SIZE = 10_000.0

# ── Daily drawdown hard stop ──────────────────────────────────────────────────
# Halt all new entries for the day if daily P&L drops below this.
DAILY_DRAWDOWN_LIMIT_PCT = 0.03        # 3% of equity ($300 on $10K)
DAILY_DRAWDOWN_LIMIT     = ACCOUNT_SIZE * DAILY_DRAWDOWN_LIMIT_PCT

# ── Max daily premium at risk ─────────────────────────────────────────────────
# Total premium that can be put at risk across ALL open options positions today.
# This is the single most important guard for options — limits catastrophic loss.
MAX_DAILY_PREMIUM_AT_RISK_PCT = 0.05   # 5% of equity ($500 on $10K)
MAX_DAILY_PREMIUM_AT_RISK     = ACCOUNT_SIZE * MAX_DAILY_PREMIUM_AT_RISK_PCT

# ── Per-trade premium risk limits ─────────────────────────────────────────────
# Max premium dollars committed per individual trade, per strategy type.
# For credit strategies, max_loss = spread width × contracts × 100 − credit received.
# For debit strategies, max_loss = premium paid.
MAX_PREMIUM_PER_TRADE_PCT = 0.015      # 1.5% of equity ($150 per trade)
MAX_PREMIUM_PER_TRADE     = ACCOUNT_SIZE * MAX_PREMIUM_PER_TRADE_PCT

# ── Concurrent options positions ──────────────────────────────────────────────
MAX_CONCURRENT_OPTIONS_POSITIONS = 5   # max open options positions at once
MAX_TRADES_PER_DAY               = 8   # max new positions opened per day

# ── IV Regime thresholds ──────────────────────────────────────────────────────
# IV Rank = (current IV - 52-week low IV) / (52-week high IV - 52-week low IV) × 100
# These thresholds determine which engine runs for a given symbol.
IV_RANK_HIGH_THRESHOLD    = 50    # IV Rank ≥ 50 → sell premium (options expensive)
IV_RANK_LOW_THRESHOLD     = 30    # IV Rank ≤ 30 → buy options  (options cheap)
# Between 30–50 is neutral; skip options, take equity trade instead

# IV Percentile thresholds (secondary confirmation)
IV_PCT_HIGH_THRESHOLD = 60        # IV Percentile ≥ 60 → elevated, lean sell
IV_PCT_LOW_THRESHOLD  = 35        # IV Percentile ≤ 35 → depressed, lean buy

# Minimum ATM IV to engage options at all (low IV = thin premium, bad fills)
MIN_ATM_IV = 0.15                 # 15% annualized IV floor

# ── Volatility Risk Premium (VRP) ─────────────────────────────────────────────
# VRP = Implied Vol − Realized Vol.
# Positive VRP (IV > RV) → premium sellers have statistical edge.
# The larger the VRP, the stronger the edge.
MIN_VRP_TO_SELL     = 2.0         # require IV at least 2 pts above realized vol to sell
REALIZED_VOL_WINDOW = 20          # 20-day realized volatility lookback

# ── Engine 1: Premium Seller ──────────────────────────────────────────────────
# Sells iron condors and credit spreads on high-IV instruments.
# Primary instruments: SPY, QQQ, IWM (index ETFs — no earnings gap risk)
PREMIUM_SELLER_SYMBOLS = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV"]

# Spread parameters
CREDIT_SPREAD_WIDTH       = 5.0   # width of credit spread in dollars (5-wide spread)
IRON_CONDOR_WING_WIDTH    = 5.0   # width of each iron condor wing in dollars
CREDIT_SPREAD_TARGET_DELTA = 0.20  # sell the ~20-delta strike (OTM, ~80% probability of profit)
IRON_CONDOR_TARGET_DELTA  = 0.20  # both wings at ~20-delta

# Exit rules (Tastytrade-validated: 50% max profit dramatically improves win rate)
CREDIT_TAKE_PROFIT_PCT    = 0.50  # close at 50% of max profit
CREDIT_STOP_LOSS_MULT     = 2.0   # close when premium worth 2× credit received (max loss = spread − credit)
CREDIT_MAX_DTE_AT_ENTRY   = 45    # never sell more than 45 DTE out
CREDIT_MIN_DTE_AT_ENTRY   = 14    # never sell less than 14 DTE (gamma risk too high)
CREDIT_CLOSE_AT_DTE       = 7     # close regardless of P&L when 7 DTE remain

# Earnings guard: never sell premium within N days of earnings (IV crush timing risk)
CREDIT_EARNINGS_BUFFER_DAYS = 3

# Minimum credit to bother: below this the fill/bid-ask cost destroys the edge
MIN_CREDIT_PER_SPREAD     = 0.50  # $0.50 per contract ($50 per 1-lot)
MIN_IRON_CONDOR_CREDIT    = 1.00  # $1.00 per condor ($100 per 1-lot)

# ── Engine 2: Directional Debit Spreads ───────────────────────────────────────
# Buys call/put debit spreads when IV is cheap AND a strong directional signal exists.
# The spread structure (buy ATM, sell OTM) reduces vega exposure vs naked options.
DEBIT_TARGET_DELTA       = 0.50   # buy the ATM strike (50-delta)
DEBIT_SHORT_DELTA        = 0.30   # sell the 30-delta strike (OTM hedge leg)
DEBIT_MAX_DTE_AT_ENTRY   = 21     # 14–21 DTE sweet spot (enough time, not too much decay)
DEBIT_MIN_DTE_AT_ENTRY   = 7      # minimum 7 DTE to avoid aggressive theta decay
DEBIT_CLOSE_AT_DTE       = 3      # close with ≥ 3 DTE remaining (gamma explosion risk)

# Profit/loss targets for debit spreads
DEBIT_TAKE_PROFIT_PCT    = 0.50   # close at 50% of max profit  (max profit = spread width − debit)
DEBIT_STOP_LOSS_PCT      = 0.50   # close when lost 50% of premium paid

# Signal quality gate: only enter debit spreads on strong underlying signals
DEBIT_MIN_SIGNAL_SCORE   = 7.5    # underlying signal score must be ≥ 7.5
DEBIT_MIN_CONFIDENCE     = 7      # minimum signal confidence 7/10

# Max debit per spread as fraction of spread width (value check)
DEBIT_MAX_DEBIT_FRACTION = 0.60   # never pay more than 60% of spread width as debit

# ── Engine 3: 0DTE SPX Scalping ───────────────────────────────────────────────
# Buys same-day-expiry SPX/SPY call or put spreads on trending market days.
# Only triggers when the broad market has a clear directional bias.
ZERO_DTE_SYMBOLS         = ["SPX", "SPY"]  # SPX preferred (no assignment risk, cash-settled)
ZERO_DTE_SPREAD_WIDTH    = 5.0    # 5-point wide SPX spread
ZERO_DTE_TARGET_DELTA    = 0.40   # slightly in-the-money for better fill probability
ZERO_DTE_SHORT_DELTA     = 0.20   # sell OTM leg

# Entry gates: only run 0DTE on trending days
ZERO_DTE_MIN_SPY_MOVE_PCT = 0.40  # SPY must be up/down ≥ 0.40% from open to trade 0DTE
ZERO_DTE_MAX_VIX          = 25.0  # don't trade 0DTE when VIX > 25 (erratic gamma)
ZERO_DTE_ENTRY_START_HOUR = 9     # entry window start (ET)
ZERO_DTE_ENTRY_START_MIN  = 45    # enter after the first 15 min of noise
ZERO_DTE_ENTRY_END_HOUR   = 11    # no new 0DTE entries after 11 AM ET
ZERO_DTE_ENTRY_END_MIN    = 0
ZERO_DTE_FORCE_CLOSE_HOUR = 15    # force close all 0DTE by 3:30 PM ET
ZERO_DTE_FORCE_CLOSE_MIN  = 30

# 0DTE profit/loss targets
ZERO_DTE_TAKE_PROFIT_PCT = 0.40   # take 40% of max profit quickly (gamma moves fast)
ZERO_DTE_STOP_LOSS_PCT   = 0.50   # cut when 50% of premium gone

# ── Greeks portfolio limits ───────────────────────────────────────────────────
# Portfolio-level Greeks exposure caps. These prevent correlated blowups.
MAX_PORTFOLIO_DELTA = 50.0        # net delta equivalent shares (SPY delta-adjusted)
MAX_PORTFOLIO_VEGA  = 200.0       # max vega exposure ($200 per 1% IV move on portfolio)
MAX_PORTFOLIO_THETA = 30.0        # min theta decay per day ($30/day positive theta target for sellers)

# Per-position Greeks caps
MAX_POSITION_DELTA  = 30.0        # max delta per single position
MAX_POSITION_VEGA   = 100.0       # max vega per single position

# ── Intraday P&L degradation ──────────────────────────────────────────────────
# Scale down new positions as daily losses accumulate.
INTRADAY_PNL_TIERS: list[tuple[float, float]] = [
    (-0.020, 0.40),   # -2.0% drawdown: size ×0.40 — one bad trade from hard stop
    (-0.015, 0.70),   # -1.5% drawdown: size ×0.70 — early warning
]

# ── VIX regime sizing ─────────────────────────────────────────────────────────
# Scale all positions when market fear is extreme.
VIX_REGIME_THRESHOLDS: list[tuple[float, float, str]] = [
    (35.0, 0.30, "crisis"),    # VIX > 35 → fear spike, only sell premium with tiny size
    (25.0, 0.55, "elevated"),  # VIX > 25 → avoid 0DTE, reduce debit trades
    (18.0, 0.85, "normal"),    # VIX > 18 → standard operating range
    ( 0.0, 1.10, "calm"),      # VIX ≤ 18 → calm market, slight boost for premium sellers
]

# ── Session timing ────────────────────────────────────────────────────────────
MARKET_OPEN_HOUR  = 9
MARKET_OPEN_MIN   = 30
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN  = 45   # no new entries after 3:45 PM ET

STUDY_START_HOUR  = 8
STUDY_START_MIN   = 30
STUDY_END_HOUR    = 9
STUDY_END_MIN     = 30

# ── Scheduler intervals ───────────────────────────────────────────────────────
POSITION_MGMT_INTERVAL_MINUTES = 2    # position monitoring cadence
SCAN_INTERVAL_MINUTES          = 5    # full strategy scan cadence
MIDDAY_SCAN_INTERVAL_MINUTES   = 15   # throttled midday scan

HIGH_VOLUME_WINDOWS = [
    (9, 30, 11, 0),    # morning momentum
    (14, 30, 15, 44),  # power hour into close
]

# Prime entry window for debit/0DTE strategies
PRIME_ENTRY_END_HOUR = 11
PRIME_ENTRY_END_MIN  = 30

# ── Universe and watchlist ────────────────────────────────────────────────────
WATCHLIST = [
    # tech
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "INTC", "QCOM", "AVGO",
    "ORCL", "CRM", "ADBE", "NFLX", "UBER", "PLTR", "CRWD", "PANW",
    # consumer
    "AMZN", "TSLA", "WMT", "COST", "NKE", "DIS", "MCD", "HD", "SBUX",
    # finance
    "JPM", "BAC", "GS", "WFC", "MS", "V", "MA", "AXP", "BLK",
    # energy
    "XOM", "CVX", "COP", "SLB", "EOG",
    # healthcare
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "AMGN", "GILD",
    # industrial
    "BA", "CAT", "GE", "HON", "RTX",
    # index ETFs (premium seller targets always included)
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "GLD",
]

# Symbols that are always scanned by the premium seller regardless of signal
PREMIUM_SELLER_ALWAYS_SCAN = ["SPY", "QQQ", "IWM"]

# ── Sector buckets ────────────────────────────────────────────────────────────
SECTOR_BUCKETS: dict[str, list[str]] = {
    "tech":       ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "INTC", "QCOM",
                   "AVGO", "ORCL", "CRM", "ADBE", "NFLX", "UBER", "PLTR", "CRWD", "PANW"],
    "consumer":   ["AMZN", "TSLA", "WMT", "COST", "NKE", "DIS", "MCD", "HD", "SBUX"],
    "finance":    ["JPM", "BAC", "GS", "WFC", "MS", "V", "MA", "AXP", "BLK"],
    "energy":     ["XOM", "CVX", "COP", "SLB", "EOG"],
    "healthcare": ["UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "AMGN", "GILD"],
    "industrial": ["BA", "CAT", "GE", "HON", "RTX"],
    "index_etf":  ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "GLD"],
}

SYMBOL_BUCKET: dict[str, str] = {
    sym: bucket
    for bucket, symbols in SECTOR_BUCKETS.items()
    for sym in symbols
}

# ── Earnings blackout ─────────────────────────────────────────────────────────
# For debit spreads: block entry within this many days of earnings.
# For premium sellers: earnings are actually a catalyst for IV crush — handled separately.
EARNINGS_BLACKOUT_DAYS_DEBIT  = 3    # avoid binary event risk for debit buyers
EARNINGS_BLACKOUT_DAYS_CREDIT = 1    # credit sellers need to be OUT before earnings

# ── Circuit breaker ───────────────────────────────────────────────────────────
CIRCUIT_BREAKER_SPY_DROP_PCT  = -2.0  # SPY down ≥ 2% from open → stand aside (wider than stocks)
CIRCUIT_BREAKER_VIX_SURGE_PCT = 20.0  # VIX up ≥ 20% intraday → stand aside

# ── Expectancy and cooling ────────────────────────────────────────────────────
SYMBOL_COOLING_LOOKBACK      = 10
SYMBOL_COOLING_MIN_WIN_RATE  = 0.30   # cool off symbol if WR < 30% on last 10 trades
DYNAMIC_WINRATE_LOOKBACK     = 10
DYNAMIC_WINRATE_THRESHOLD    = 0.45
MAX_CONSECUTIVE_LOSSES       = 3      # after 3 consecutive losses, stand aside

# ── Screener ──────────────────────────────────────────────────────────────────
SCREENER_MIN_PRICE    = 5.0
SCREENER_MAX_PRICE    = 1000.0
UNIVERSE_MAX_SYMBOLS  = 80

# ── Email / SMTP ──────────────────────────────────────────────────────────────
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "okaforandrew416@gmail.com")
SMTP_HOST       = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER       = os.getenv("SMTP_USER", "")
SMTP_PASS       = os.getenv("SMTP_PASS", "")

# ── Persistence ───────────────────────────────────────────────────────────────
DB_PATH  = "options_trading.db"
LOG_FILE = "bot.log"

# ── Risk-free rate for Black-Scholes ─────────────────────────────────────────
RISK_FREE_RATE = 0.05   # approximate 5% annualized (update periodically)

# ── Minimum option liquidity gates ───────────────────────────────────────────
MIN_OPTION_OPEN_INTEREST = 100    # skip strikes with OI < 100 contracts
MIN_OPTION_VOLUME        = 10     # skip strikes with daily volume < 10
MAX_OPTION_BID_ASK_PCT   = 0.10   # skip options with bid-ask spread > 10% of mid

# ── Options constant aliases (readable names used across the stack) ────────────
# These resolve name differences between the original equity bot and the new
# options engine so all modules can use the most descriptive name.
CREDIT_CLOSE_DTE_DAYS     = CREDIT_CLOSE_AT_DTE    # 7 — close credits at ≤7 DTE
DEBIT_CLOSE_DTE_DAYS      = DEBIT_CLOSE_AT_DTE     # 3 — close debits at ≤3 DTE
CREDIT_STOP_LOSS_MULTIPLIER = CREDIT_STOP_LOSS_MULT  # 2.0 — stop at 2× credit received
MAX_OPEN_OPTIONS_POSITIONS  = MAX_CONCURRENT_OPTIONS_POSITIONS  # 5 — position cap

# Maximum new options positions opened per single scan cycle (prevents burst entries)
MAX_OPTIONS_ENTRIES_PER_CYCLE = 2

# Maximum contracts per individual options trade (hard cap regardless of sizing)
MAX_CONTRACTS_PER_TRADE = 5

# Minimum underlying price to consider for options (sub-$10 stocks have illiquid chains)
MIN_UNDERLYING_PRICE = 10.0

# Earnings blackout default window (used by EarningsMixin generic check)
EARNINGS_BLACKOUT_DAYS = max(EARNINGS_BLACKOUT_DAYS_DEBIT, EARNINGS_BLACKOUT_DAYS_CREDIT)

# Maximum 10-day return correlation between an incoming and existing position
MAX_HOLDING_CORRELATION = 0.80

# UVXY surge threshold for the circuit breaker (UVXY up ≥ 5% → stand aside)
CIRCUIT_BREAKER_UVXY_SURGE_PCT = 5.0

# ── Screener slot allocation ──────────────────────────────────────────────────
# Controls how many symbols are drawn from each screener bucket when building
# the trading universe.
SCREENER_SNAPSHOT_SLOTS = 20   # Alpaca snapshot (momentum leaders)
SCREENER_ACTIVES_SLOTS  = 10   # most-active by volume
SCREENER_GAINERS_SLOTS  = 10   # top percentage gainers

# ── Signal score thresholds ───────────────────────────────────────────────────
NORMAL_MIN_SIGNAL_SCORE  = 6.0   # minimum score to enter during core hours
MIDDAY_MIN_SIGNAL_SCORE  = 7.0   # higher bar during low-conviction midday window
MIN_SIGNAL_SCORE_TO_AI   = 5.0   # minimum score to pass to AI for analysis
MIN_SIGNAL_CONFIDENCE    = 6     # minimum confidence (1–10 scale) for any entry
HIGH_CONVICTION_THRESHOLD = 9    # score at or above this triggers full-size entry
MIN_REWARD_TO_RISK       = 2.0   # minimum acceptable reward-to-risk ratio
MIN_VOL_RATIO_ENTRY      = 1.5   # volume must be ≥1.5× 20-day average at entry

# ── Portfolio heat and exposure limits (equity bot risk manager) ───────────────
MAX_CONCURRENT_POSITIONS = 8             # legacy equity bot concurrent position cap
MAX_DAILY_CAPITAL        = ACCOUNT_SIZE * 0.80  # max capital deployed in a day
MAX_PORTFOLIO_HEAT_PCT   = 0.06          # halt new entries if total risk > 6% of equity
MAX_TOTAL_EXPOSURE_PCT   = 0.90          # never deploy more than 90% of equity at once

# Legacy alias so risk/manager.py can use the same thresholds as the options stack
VOL_REGIME_THRESHOLDS = VIX_REGIME_THRESHOLDS

# ── Per-trade sizing constants (equity bot legacy) ────────────────────────────
MAX_RISK_PER_TRADE_PCT   = 0.02          # max 2% of equity risked per equity trade
MAX_RISK_PER_TRADE       = ACCOUNT_SIZE * MAX_RISK_PER_TRADE_PCT
MAX_POSITION_SIZE        = 0.10          # max 10% of equity in a single stock position
MIN_POSITION_SIZE        = 1             # minimum 1 share (floor for small accounts)
MAX_SPREAD_PCT           = 0.03          # skip equity with bid-ask > 3% of price
MAX_TRADEABLE_ATR_PCT    = 0.05          # skip equity whose ATR > 5% of price (too erratic)

# ── Stop and take-profit defaults (equity bot) ────────────────────────────────
DEFAULT_STOP_LOSS_PCT    = 0.07          # 7% default stop loss for equity trades
DEFAULT_TAKE_PROFIT_PCT  = 0.15          # 15% default take profit for equity trades
ATR_STOP_MULTIPLIER      = 1.5           # stop = entry − ATR × 1.5
TRAILING_STOP_TRIGGER_PCT  = 0.05        # activate trailing stop after 5% gain
TRAILING_STOP_DISTANCE_PCT = 0.03        # trail 3% below the high-water mark
BREAKEVEN_TRIGGER_PCT      = 0.015       # move stop to breakeven after 1.5% gain

# ── Gap-and-go parameters (equity bot morning scanner) ───────────────────────
GAP_AND_GO_MIN_PCT     = 0.02    # minimum gap size to qualify as a gap-and-go setup
GAP_AND_GO_MAX_PCT     = 0.10    # gaps > 10% are too extended, skip
GAP_AND_GO_CUTOFF_HOUR = 10      # no new gap-and-go entries after 10:00 ET
GAP_AND_GO_CUTOFF_MIN  = 0

# ── Signal confidence scaling (equity bot) ────────────────────────────────────
CONFIDENCE_SIZE_SCALE      = 0.10   # scale position size by confidence above minimum
CONFIDENCE_DRIFT_THRESHOLD = 0.20   # drift this far from target before rebalancing
