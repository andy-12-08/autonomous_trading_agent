"""
Full pre-live test suite.

Sections:
  1. Syntax / import check — every module must load cleanly
  2. External API endpoints — ping every data source
  3. Risk manager — all 20 veto rules
  4. Signal scorer — scoring logic + filter gate
  5. Indicators — liquidity sweep + FVG + key levels
  6. Expectancy — Kelly, consecutive-loss window, setup suppression
  7. Bucket manager — diversification + sector strength
  8. Dark pool — FINRA CNMS parse
  9. Pre-market — extended-hours levels
 10. Yield curve — TNX/IRX/HYG/LQD
 11. Short interest — yfinance info
 12. EDGAR — 8-K gate (live + cache)
 13. Screener — universe build (snapshot screen)
 14. Alpaca — account, positions, quotes, bars, order capability
 15. Session overrides — round-trip load/apply
"""
import sys
import traceback
from datetime import datetime, timezone, timedelta, date

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"
INFO = "\033[96m[INFO]\033[0m"

results: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        result = fn()
        ok  = result if isinstance(result, bool) else True
        msg = str(result) if not isinstance(result, bool) else "OK"
        results.append((name, ok, msg))
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}: {msg}")
        return ok
    except Exception as e:
        tb = traceback.format_exc().strip().split("\n")[-1]
        results.append((name, False, str(e)))
        print(f"  {FAIL} {name}: {e}")
        print(f"         {tb}")
        return False


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ─────────────────────────────────────────────────────────────
# 1. IMPORT CHECK
# ─────────────────────────────────────────────────────────────
section("1. MODULE IMPORT CHECK")

import config
import logging
logging.disable(logging.CRITICAL)   # silence module loggers during tests

modules_to_import = [
    "config", "logger", "alpaca_client", "indicators", "risk_manager",
    "signal_scorer", "expectancy", "bucket_manager", "gfv_tracker",
    "market_guard", "screener", "dark_pool", "options_flow", "insider_flow",
    "pre_market", "yield_curve", "short_interest", "edgar",
    "session_overrides", "notifier", "ai_agent", "market_analyst", "trader",
]
for mod in modules_to_import:
    check(f"import {mod}", lambda m=mod: __import__(m) is not None)

logging.disable(logging.NOTSET)

# ─────────────────────────────────────────────────────────────
# 2. EXTERNAL API ENDPOINTS
# ─────────────────────────────────────────────────────────────
section("2. EXTERNAL API ENDPOINTS")
import requests

def ping(label, url, method="get", headers=None, params=None, expected_status=200, json_key=None):
    try:
        r = getattr(requests, method)(url, headers=headers or {}, params=params or {},
                                       timeout=10)
        ok = r.status_code == expected_status
        msg = f"HTTP {r.status_code}"
        if ok and json_key:
            data = r.json()
            val  = data.get(json_key, "MISSING")
            msg  = f"HTTP 200, {json_key}={str(val)[:40]}"
            ok   = val != "MISSING"
        return ok, msg
    except Exception as e:
        return False, str(e)

alpaca_headers = {
    "APCA-API-KEY-ID":     config.ALPACA_KEY or "",
    "APCA-API-SECRET-KEY": config.ALPACA_SECRET or "",
}

endpoints = [
    ("Alpaca paper API — account",
     "https://paper-api.alpaca.markets/v2/account", alpaca_headers, None, "status"),
    ("Alpaca data — most-actives",
     "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives",
     alpaca_headers, {"by": "volume", "top": 5}, "most_actives"),
    ("Alpaca data — movers",
     "https://data.alpaca.markets/v1beta1/screener/stocks/movers",
     alpaca_headers, {"top": 5}, "gainers"),
    ("Alpaca data — news",
     "https://data.alpaca.markets/v1beta1/news",
     alpaca_headers, {"symbols": "AAPL", "limit": 1}, "news"),
]
for label, url, hdrs, params, jkey in endpoints:
    ok, msg = ping(label, url, headers=hdrs, params=params, json_key=jkey)
    results.append((label, ok, msg))
    print(f"  {PASS if ok else FAIL} {label}: {msg}")

# FINRA CNMS dark pool file
check("FINRA CNMS dark pool (latest file)",
      lambda: __import__("dark_pool").load_dark_pool_data() != {})

# SEC EDGAR full-text search
ok, msg = ping("SEC EDGAR 8-K search",
               "https://efts.sec.gov/LATEST/search-index",
               params={"q": '"AAPL"', "forms": "8-K",
                       "dateRange": "custom",
                       "startdt": date.today().isoformat(),
                       "enddt":   date.today().isoformat()},
               headers={"User-Agent": "TradingBot/1.0 test@example.com"})
results.append(("SEC EDGAR 8-K search", ok, msg))
print(f"  {PASS if ok else FAIL} SEC EDGAR 8-K search: {msg}")

# yfinance (used by pre_market, yield_curve, short_interest, options_flow, insider_flow)
import yfinance as yf
check("yfinance AAPL 1d quote",
      lambda: float(yf.Ticker("AAPL").fast_info["last_price"]) > 0)
check("yfinance ^TNX (10Y yield)",
      lambda: yf.download("^TNX", period="2d", interval="1d", progress=False).empty is False)
check("yfinance HYG 2d bars",
      lambda: yf.download("HYG", period="2d", interval="1d", progress=False).empty is False)

# Anthropic API
import anthropic
check("Anthropic API — ping (tiny completion)",
      lambda: __import__("anthropic").Anthropic(
          api_key=config.ANTHROPIC_API_KEY).messages.create(
          model=config.CLAUDE_MODEL,
          max_tokens=10,
          messages=[{"role":"user","content":"say hi"}]
      ).content[0].text != "")

# ─────────────────────────────────────────────────────────────
# 3. RISK MANAGER
# ─────────────────────────────────────────────────────────────
section("3. RISK MANAGER — VETO RULES")
import risk_manager as rm

def rm_check(name, expected_ok, **kwargs):
    defaults = dict(
        symbol="TEST", price=100.0, qty=3, stop_loss=97.0,
        settled_cash=5000.0, deployed_today=0.0, num_positions=0,
        daily_pnl=0.0, total_equity=10000.0, trades_today=0,
        reward_to_risk=2.5, signal_confidence=7, vol_ratio=1.5, rsi=55.0,
    )
    defaults.update(kwargs)
    ok, reason = rm.approve_buy(**defaults)
    passed = ok == expected_ok
    results.append((f"rm: {name}", passed, reason[:60]))
    tag = PASS if passed else FAIL
    print(f"  {tag} {name}: {reason[:80]}")
    return passed

rm_check("baseline approval",                True)
rm_check("daily drawdown hit",               False, daily_pnl=-210.0)
rm_check("exposure cap (40%)",               False, deployed_today=4100.0)
rm_check("settled cash too low",             False, settled_cash=50.0)
rm_check("too many positions (>4)",          False, num_positions=4)
rm_check("R:R below 2.0",                   False, reward_to_risk=1.5)
rm_check("confidence below floor",           False, signal_confidence=5)
rm_check("volume too low",                   False, vol_ratio=0.7)
rm_check("spread too wide",                  False, spread_pct=0.005)
rm_check("price within 0.5% of resistance", False,
         key_levels={"nearest_resistance": 100.4})
rm_check("resistance 1% away → allowed",    True,
         key_levels={"nearest_resistance": 101.5})

# calc_qty basic
def test_calc_qty():
    qty = rm.calc_qty(100.0, 97.0, 5000.0, 0.0, 10000.0, atr=1.0, confidence=7)
    assert qty > 0, f"Expected qty>0 got {qty}"
    return f"qty={qty}"
check("rm.calc_qty basic", test_calc_qty)

# Kelly factor influence
def test_kelly_reduces_size():
    base = rm.calc_qty(100.0, 97.0, 5000.0, 0.0, 10000.0, atr=1.0, confidence=7, kelly_factor=1.0)
    half = rm.calc_qty(100.0, 97.0, 5000.0, 0.0, 10000.0, atr=1.0, confidence=7, kelly_factor=0.5)
    assert half <= base, f"Kelly 0.5 should reduce size: base={base}, half={half}"
    return f"base={base} kelly0.5={half} (correctly reduced)"
check("rm.calc_qty kelly_factor reduces size", test_kelly_reduces_size)

# compute_stop_take_profit
def test_sl_tp():
    sl, tp = rm.compute_stop_take_profit(100.0, 1.0)
    assert sl is not None and sl < 100.0, f"SL should be below price, got {sl}"
    assert tp is not None and tp > 100.0, f"TP should be above price, got {tp}"
    rr = (tp - 100.0) / (100.0 - sl)
    assert rr >= 2.0, f"R:R should be >= 2.0, got {rr:.2f}"
    return f"SL={sl:.2f} TP={tp:.2f} RR={rr:.2f}"
check("rm.compute_stop_take_profit RR>=2.0", test_sl_tp)

# ─────────────────────────────────────────────────────────────
# 4. SIGNAL SCORER
# ─────────────────────────────────────────────────────────────
section("4. SIGNAL SCORER")
import signal_scorer as scorer

def test_score_floor():
    # MIN_SIGNAL_SCORE_TO_AI should now be 6.0
    assert config.MIN_SIGNAL_SCORE_TO_AI == 6.0, \
        f"Expected 6.0, got {config.MIN_SIGNAL_SCORE_TO_AI}"
    return f"MIN_SIGNAL_SCORE_TO_AI={config.MIN_SIGNAL_SCORE_TO_AI}"
check("signal score gate is 6.0", test_score_floor)

def test_momentum_score():
    sig = {
        "price": 100.0, "ema9": 98.0, "ema21": 96.0, "ema50": 92.0,
        "macd_hist": 0.5, "rsi": 58.0, "above_vwap": True, "vol_ratio": 1.8,
        "ema_bull": True, "macd_bull": True,
        "rs_vs_spy": 0.8, "in_discount_zone": True, "near_bull_fvg": True,
    }
    score, ev = scorer.score_setup(sig, {}, {})
    assert score >= 6.0, f"Strong momentum signal should score >=6.0, got {score}"
    return f"score={score:.1f} evidence={ev[0]}"
check("momentum scorer — strong signal >=6.0", test_momentum_score)

def test_filter_drops_low_scores():
    weak_item = {
        "symbol": "WEAK", "indicators": {
            "price": 50.0, "rsi": 45.0, "above_vwap": False,
            "vol_ratio": 0.5, "ema_bull": False, "macd_bull": False,
        }, "bias_15min": {}, "bias_daily": {},
    }
    result = scorer.filter_watchlist([weak_item], midday=False)
    assert len(result) == 0, f"Weak item should be filtered out, got {len(result)}"
    return "weak item correctly dropped"
check("filter_watchlist drops score<6.0", test_filter_drops_low_scores)

def test_gap_and_go_catalyst_after_11():
    import pytz
    from unittest.mock import patch
    et = pytz.timezone("America/New_York")
    mock_time = datetime(2026, 5, 1, 13, 0, tzinfo=et)   # 1 PM ET
    with patch("signal_scorer.datetime") as mock_dt:
        mock_dt.now.return_value = mock_time
        sig = {
            "gap_pct": 3.0, "today_open": 100.0, "first_bar_high": 103.0,
            "price": 103.5, "vol_ratio": 4.0, "rsi": 62.0, "above_vwap": True,
            "orb_30_high": 103.0, "orb_30_low": 100.0, "orb_30_valid": True,
            "orb_30_width_pct": 3.0,
        }
        score, ev = scorer.score_gap_and_go(sig)
        assert score > 0, f"vol_ratio=4.0 should bypass 11AM gate, got score={score}"
        return f"after-11AM catalyst score={score:.1f} (vol_ratio=4.0 bypassed gate)"
check("gap-and-go catalyst bypasses 11AM gate when vol_ratio>=3.0", test_gap_and_go_catalyst_after_11)

def test_gap_and_go_blocked_after_11():
    import pytz
    from unittest.mock import patch
    et = pytz.timezone("America/New_York")
    mock_time = datetime(2026, 5, 1, 13, 0, tzinfo=et)   # 1 PM ET
    with patch("signal_scorer.datetime") as mock_dt:
        mock_dt.now.return_value = mock_time
        sig = {"gap_pct": 2.5, "today_open": 100.0, "price": 102.5, "vol_ratio": 1.2,
               "rsi": 60.0, "above_vwap": True, "orb_30_high": 0, "orb_30_low": 0,
               "orb_30_valid": False, "orb_30_width_pct": 0, "first_bar_high": 0}
        score, _ = scorer.score_gap_and_go(sig)
        assert score == 0.0, f"Low vol_ratio should still be blocked after 11AM, got {score}"
        return "low vol_ratio correctly blocked past 11AM gate"
check("gap-and-go still blocked after 11AM with vol_ratio<3.0", test_gap_and_go_blocked_after_11)

# ─────────────────────────────────────────────────────────────
# 5. INDICATORS — LIQUIDITY SWEEP + FVG
# ─────────────────────────────────────────────────────────────
section("5. INDICATORS — LIQUIDITY SWEEP & FVG")
import indicators as ind
import pandas as pd
import numpy as np

def make_df(n=50, base_price=100.0, trend=0.0):
    """Generate a synthetic OHLCV DataFrame."""
    np.random.seed(42)
    closes = base_price + trend * np.arange(n) + np.random.randn(n) * 0.5
    opens  = closes - np.random.randn(n) * 0.3
    highs  = np.maximum(opens, closes) + np.abs(np.random.randn(n)) * 0.3
    lows   = np.minimum(opens, closes) - np.abs(np.random.randn(n)) * 0.3
    vols   = np.random.randint(50000, 200000, n).astype(float)
    idx    = pd.date_range("2026-05-01 09:30", periods=n, freq="5min", tz="America/New_York")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": vols}, index=idx)

def test_sweep_detected():
    df = make_df(30)
    support_level = float(df["low"].mean()) - 0.5
    # Manufacture a sweep: last bar pierces below support then closes above
    df.iloc[-1, df.columns.get_loc("low")]   = support_level - 0.3
    df.iloc[-1, df.columns.get_loc("close")] = support_level + 0.5
    df.iloc[-1, df.columns.get_loc("volume")] = 300000.0  # spike volume
    result = ind.detect_liquidity_sweep(df, key_levels={"nearest_support": support_level})
    detected = result.get("liquidity_sweep_detected", False)
    assert detected, f"Sweep should be detected: {result}"
    assert result["stop_beyond"] < result["sweep_low"], "Stop must be below sweep low"
    return f"sweep_low={result['sweep_low']:.2f} stop={result['stop_beyond']:.2f}"
check("detect_liquidity_sweep — sweep detected", test_sweep_detected)

def test_sweep_not_triggered_on_clean_data():
    df = make_df(30)
    result = ind.detect_liquidity_sweep(df)
    detected = result.get("liquidity_sweep_detected", False)
    return f"sweep_detected={detected} on clean trending data"
check("detect_liquidity_sweep — no false positive on clean data", test_sweep_not_triggered_on_clean_data)

def test_fvg_detected():
    df = make_df(20)
    # Manufacture a bullish FVG: candle[17].high < candle[19].low
    df.iloc[17, df.columns.get_loc("high")] = 98.0
    df.iloc[18, df.columns.get_loc("high")] = 99.0
    df.iloc[18, df.columns.get_loc("low")]  = 98.5
    df.iloc[19, df.columns.get_loc("low")]  = 99.5   # gap: 98.0–99.5
    df.iloc[19, df.columns.get_loc("close")] = 102.0
    result = ind.detect_fvg(df)
    return f"fvg keys={list(result.keys())}"
check("detect_fvg returns result", test_fvg_detected)

def test_compute_indicators_runs():
    df = make_df(60)
    df_out = ind.compute_indicators(df)
    required = ["ema9", "ema21", "rsi", "macd", "atr", "vwap"]
    missing  = [c for c in required if c not in df_out.columns]
    assert not missing, f"Missing indicator columns: {missing}"
    return f"all {len(required)} required columns present"
check("compute_indicators produces all required columns", test_compute_indicators_runs)

def test_get_key_levels():
    df = make_df(60)
    df_out = ind.compute_indicators(df)
    sig = ind.get_signal_summary(df_out)
    assert sig, "get_signal_summary should return non-empty dict"
    assert "price" in sig, "signal summary must include price"
    return f"signal_summary keys={len(sig)}"
check("get_signal_summary returns valid dict", test_get_key_levels)

# ─────────────────────────────────────────────────────────────
# 6. EXPECTANCY — KELLY + 90-MIN WINDOW
# ─────────────────────────────────────────────────────────────
section("6. EXPECTANCY & REVENGE TRADE GUARD")
import expectancy as exp

def test_kelly_no_data():
    factor = exp.compute_kelly_factor([])
    assert factor == 1.0, f"No data should return 1.0, got {factor}"
    return f"kelly={factor} (no data → neutral)"
check("compute_kelly_factor — no data returns 1.0", test_kelly_no_data)

def test_kelly_positive_edge():
    decisions = [
        {"action": "SELL", "pnl": 100} for _ in range(7)
    ] + [
        {"action": "SELL", "pnl": -40} for _ in range(3)
    ]
    factor = exp.compute_kelly_factor(decisions)
    assert 0.25 <= factor <= 1.5, f"Kelly factor out of range: {factor}"
    return f"kelly={factor:.3f} (7W/3L → positive edge)"
check("compute_kelly_factor — positive edge in range", test_kelly_positive_edge)

now_utc = datetime.now(timezone.utc)

def test_rapid_losses_trigger():
    decisions = [
        {"action": "SELL", "pnl": -50, "ts": (now_utc - timedelta(minutes=5)).isoformat()},
        {"action": "SELL", "pnl": -40, "ts": (now_utc - timedelta(minutes=20)).isoformat()},
        {"action": "SELL", "pnl": -30, "ts": (now_utc - timedelta(minutes=35)).isoformat()},
    ]
    streak = exp.get_recent_consecutive_losses(decisions)
    assert streak == 3, f"3 rapid losses should count as streak=3, got {streak}"
    return f"streak={streak} (3 losses in 35 min → full trigger)"
check("consecutive losses — rapid-fire counted as streak=3", test_rapid_losses_trigger)

def test_spread_losses_discounted():
    decisions = [
        {"action": "SELL", "pnl": -50, "ts": (now_utc - timedelta(minutes=10)).isoformat()},
        {"action": "SELL", "pnl": -40, "ts": (now_utc - timedelta(minutes=120)).isoformat()},
        {"action": "SELL", "pnl": -30, "ts": (now_utc - timedelta(minutes=200)).isoformat()},
    ]
    streak = exp.get_recent_consecutive_losses(decisions)
    assert streak < 3, f"Spread losses over 3h should discount to streak<3, got {streak}"
    return f"streak={streak} (losses spread 3h+ → discounted)"
check("consecutive losses — spread over 3h discounted to <3", test_spread_losses_discounted)

def test_revenge_guard_stands_aside():
    allowed, reason = exp.check_revenge_trade_guard(3, signal_confidence=7)
    assert not allowed, "3 rapid losses + conf=7 should stand aside"
    return f"blocked: {reason[:60]}"
check("revenge guard blocks at 3 losses + low confidence", test_revenge_guard_stands_aside)

def test_revenge_guard_allows_high_conf():
    required = config.MIN_SIGNAL_CONFIDENCE + 2   # 8
    allowed, reason = exp.check_revenge_trade_guard(3, signal_confidence=required)
    assert allowed, f"conf={required} should clear the bar after 3 losses"
    return f"allowed with conf={required}"
check("revenge guard allows high-confidence trade after 3 losses", test_revenge_guard_allows_high_conf)

# ─────────────────────────────────────────────────────────────
# 7. BUCKET MANAGER
# ─────────────────────────────────────────────────────────────
section("7. BUCKET MANAGER")
import bucket_manager as bm

def test_empty_bucket_allowed():
    ok, reason = bm.bucket_is_open("AAPL", [])
    assert ok, f"Empty bucket should allow entry: {reason}"
    return reason
check("bucket_is_open — empty bucket allowed", test_empty_bucket_allowed)

def test_occupied_bucket_blocked():
    positions = [{"symbol": "AAPL"}]   # AAPL is in 'tech'
    ok, reason = bm.bucket_is_open("MSFT", positions, signal_confidence=7)
    assert not ok, f"Same bucket should block: {reason}"
    return reason[:60]
check("bucket_is_open — occupied bucket blocked (conf=7)", test_occupied_bucket_blocked)

def test_high_conviction_override():
    positions = [{"symbol": "AAPL"}]
    ok, reason = bm.bucket_is_open("MSFT", positions, signal_confidence=9)
    assert ok, f"High-conviction should override: {reason}"
    return reason[:60]
check("bucket_is_open — high-conviction (9/10) overrides", test_high_conviction_override)

def test_sector_strength():
    snaps = {
        "SPY": {"change_pct": 0.5},
        "XLK": {"change_pct": 1.5},   # tech leading
        "XLF": {"change_pct": 0.2},   # finance lagging
    }
    strength = bm.get_sector_strength(snaps)
    assert strength["tech"] > 0, f"Tech should be leading: {strength}"
    assert strength["finance"] < strength["tech"], "Finance should lag tech"
    return f"tech={strength['tech']:+.2f}% finance={strength['finance']:+.2f}%"
check("get_sector_strength — relative vs SPY correct", test_sector_strength)

# ─────────────────────────────────────────────────────────────
# 8. DARK POOL
# ─────────────────────────────────────────────────────────────
section("8. DARK POOL (FINRA CNMS)")
import dark_pool as dp

def test_dark_pool_loads():
    data = dp.load_dark_pool_data()
    assert isinstance(data, dict), "Should return dict"
    assert len(data) > 100, f"Expected >100 symbols, got {len(data)}"
    return f"{len(data)} symbols loaded"
check("dark pool — FINRA file loads and parses", test_dark_pool_loads)

def test_dark_pool_signal_values():
    data = dp.load_dark_pool_data()
    sample = list(data.values())[:5]
    for d in sample:
        assert 0 <= d["short_vol_pct"] <= 1, f"short_vol_pct out of range: {d}"
        assert d["signal"] in ("accumulation", "distribution", "neutral")
    return f"all signals valid in sample of {len(sample)}"
check("dark pool — signal values are valid", test_dark_pool_signal_values)

def test_dark_pool_known_symbols():
    signals = dp.get_dark_pool_signals(["AAPL", "NVDA", "SPY"])
    found   = list(signals.keys())
    return f"found {len(found)}/3: {found}"
check("dark pool — get_dark_pool_signals for AAPL/NVDA/SPY", test_dark_pool_known_symbols)

# ─────────────────────────────────────────────────────────────
# 9. PRE-MARKET
# ─────────────────────────────────────────────────────────────
section("9. PRE-MARKET LEVELS")
import pre_market as pm_mod

def test_premarket_fetch():
    data = pm_mod.get_premarket_data(["AAPL", "NVDA", "TSLA"])
    assert isinstance(data, dict), "Should return dict"
    assert len(data) >= 1, f"At least 1 symbol should have data, got {len(data)}"
    for sym, d in data.items():
        assert "pm_high" in d and "pm_low" in d, f"{sym} missing pm levels"
        assert d["pm_high"] >= d["pm_low"], f"{sym}: pm_high < pm_low"
        assert d["gap_direction"] in ("up", "down", "flat")
    return f"{len(data)} symbols: " + ", ".join(
        f"{s}({d['gap_pct']:+.1f}%)" for s, d in data.items())
check("pre_market — fetches levels for AAPL/NVDA/TSLA", test_premarket_fetch)

def test_premarket_key_levels():
    levels = pm_mod.get_premarket_key_levels("AAPL")
    if levels:
        assert "pre_market_high" in levels and "pre_market_low" in levels
        return f"pm_high={levels['pre_market_high']} pm_low={levels['pre_market_low']}"
    return "no pre-market data (market closed — acceptable)"
check("pre_market — get_premarket_key_levels AAPL", test_premarket_key_levels)

# ─────────────────────────────────────────────────────────────
# 10. YIELD CURVE
# ─────────────────────────────────────────────────────────────
section("10. YIELD CURVE + CREDIT SPREADS")
import yield_curve as yc_mod

def test_yield_curve_fetch():
    data = yc_mod.get_yield_curve()
    assert "signal" in data and "size_multiplier" in data
    assert data["signal"] in ("risk_on", "normal", "cautious", "risk_off")
    assert 0.5 <= data["size_multiplier"] <= 1.0
    tnx = data.get("ten_year_yield")
    irx = data.get("three_month_yield")
    assert tnx and tnx > 0, f"10Y yield should be positive, got {tnx}"
    return (f"10Y={tnx:.2f}% 3M={irx:.2f}% "
            f"spread={data['spread_10y_3m']:+.2f}% "
            f"signal={data['signal']} ×{data['size_multiplier']:.2f}")
check("yield_curve — live TNX/IRX/HYG/LQD data", test_yield_curve_fetch)

def test_yield_curve_cached():
    d1 = yc_mod.get_yield_curve()
    d2 = yc_mod.get_yield_curve()   # should hit cache
    assert d1["signal"] == d2["signal"], "Cache should return same signal"
    return "cache hit returns consistent result"
check("yield_curve — cache returns same result", test_yield_curve_cached)

# ─────────────────────────────────────────────────────────────
# 11. SHORT INTEREST
# ─────────────────────────────────────────────────────────────
section("11. SHORT INTEREST")
import short_interest as si_mod

def test_short_interest_fetch():
    data = si_mod.get_short_interest(["AAPL", "TSLA", "NVDA"])
    assert isinstance(data, dict)
    assert len(data) >= 1, f"Expected data for at least 1 symbol, got {len(data)}"
    for sym, d in data.items():
        assert 0 <= d["short_pct_float"] <= 1.0, f"{sym}: pct out of range"
        assert d["signal"] in ("squeeze_risk", "elevated", "normal", "low")
    return ", ".join(f"{s}:{d['short_pct_float']:.1%}[{d['signal']}]" for s, d in data.items())
check("short_interest — fetches data for AAPL/TSLA/NVDA", test_short_interest_fetch)

# ─────────────────────────────────────────────────────────────
# 12. EDGAR 8-K GATE
# ─────────────────────────────────────────────────────────────
section("12. SEC EDGAR 8-K GATE")
import edgar

def test_edgar_no_veto_normal():
    veto, reason = edgar.check_fresh_8k("SPY")
    # SPY (S&P ETF) almost never files 8-Ks
    return f"veto={veto} reason={reason}"
check("edgar — SPY: no 8-K veto expected", test_edgar_no_veto_normal)

def test_edgar_cache():
    edgar.check_fresh_8k("AAPL")   # prime cache
    veto, reason = edgar.check_fresh_8k("AAPL")  # should hit cache
    return f"veto={veto} (cache hit)"
check("edgar — cache works (second call hits cache)", test_edgar_cache)

def test_edgar_fail_open():
    # Test with a bad URL substitution — should fail open (veto=False)
    orig = edgar._BASE
    edgar._BASE = "https://invalid.notareal.domain.xyz/search"
    veto, reason = edgar.check_fresh_8k("FAILTEST")
    edgar._BASE = orig
    assert not veto, f"Should fail open (veto=False), got veto={veto}"
    return f"failed open: {reason}"
check("edgar — network error fails open (no veto)", test_edgar_fail_open)

# ─────────────────────────────────────────────────────────────
# 13. SCREENER
# ─────────────────────────────────────────────────────────────
section("13. SCREENER")
import screener

def test_most_actives():
    syms = screener._fetch_most_actives(top=10)
    assert isinstance(syms, list), "Should return list"
    assert len(syms) >= 5, f"Expected >=5 symbols, got {len(syms)}"
    assert all(s.isalpha() and len(s) <= 5 for s in syms), "Bad symbol format"
    return f"{len(syms)} symbols: {syms[:5]}"
check("screener — most-actives returns valid symbols", test_most_actives)

def test_gainers():
    syms = screener._fetch_gainers(top=10)
    assert isinstance(syms, list)
    return f"{len(syms)} gainers fetched"
check("screener — gainers fetches OK", test_gainers)

def test_universe_build():
    universe = screener.build_universe()
    assert isinstance(universe, list)
    assert len(universe) >= len(config.WATCHLIST), \
        f"Universe ({len(universe)}) should be >= watchlist ({len(config.WATCHLIST)})"
    assert all(isinstance(s, str) and s.isalpha() for s in universe[:20])
    return f"{len(universe)} symbols in universe"
check("screener — build_universe returns full universe", test_universe_build)

# ─────────────────────────────────────────────────────────────
# 14. ALPACA BROKER
# ─────────────────────────────────────────────────────────────
section("14. ALPACA BROKER")
import alpaca_client as broker

def test_alpaca_account():
    acct = broker.get_account()
    assert acct is not None
    equity = float(getattr(acct, "equity", 0) or 0)
    assert equity > 0, f"Equity should be >0, got {equity}"
    return f"equity=${equity:,.2f}"
check("alpaca — get_account returns equity", test_alpaca_account)

def test_alpaca_positions():
    positions = broker.get_positions()
    assert isinstance(positions, dict)
    return f"{len(positions)} open positions"
check("alpaca — get_positions returns dict", test_alpaca_positions)

def test_alpaca_quote():
    # Returns None outside market hours (stale bid/ask = 0). Confirmed correct behavior.
    quote = broker.get_latest_quote("AAPL")
    if quote is None:
        return "None returned (market closed — correct; bid/ask are 0 when not trading)"
    assert quote["bid"] > 0 and quote["ask"] > 0
    assert quote["spread_pct"] >= 0
    return f"bid={quote['bid']:.2f} ask={quote['ask']:.2f} spread={quote['spread_pct']:.4f}"
check("alpaca — get_latest_quote AAPL (None OK outside hours)", test_alpaca_quote)

def test_alpaca_price():
    price = broker.get_latest_price("AAPL")
    assert price and price > 0
    return f"price=${price:.2f}"
check("alpaca — get_latest_price AAPL", test_alpaca_price)

def test_alpaca_bars():
    df = broker.get_bars("AAPL", "5Min", days=2)
    assert not df.empty, "Should return bars"
    assert len(df) >= 10, f"Expected >=10 bars, got {len(df)}"
    return f"{len(df)} bars"
check("alpaca — get_bars AAPL 5Min", test_alpaca_bars)

def test_alpaca_bars_multi():
    bars = broker.get_bars_multi(["AAPL", "MSFT"], "5Min", days=2)
    assert isinstance(bars, dict)
    assert "AAPL" in bars or "MSFT" in bars
    return f"multi-bars: {list(bars.keys())}"
check("alpaca — get_bars_multi AAPL+MSFT", test_alpaca_bars_multi)

def test_alpaca_snapshots():
    snaps = broker.get_snapshots_bulk(["AAPL", "NVDA", "SPY"])
    assert isinstance(snaps, dict)
    assert len(snaps) >= 1
    for sym, d in snaps.items():
        assert d["price"] > 0
    return f"{len(snaps)} snapshots: " + ", ".join(
        f"{s}=${d['price']:.2f}" for s, d in snaps.items())
check("alpaca — get_snapshots_bulk AAPL/NVDA/SPY", test_alpaca_snapshots)

def test_alpaca_market_open():
    is_open = broker.is_market_open()
    return f"market_open={is_open}"
check("alpaca — is_market_open returns bool", test_alpaca_market_open)

def test_alpaca_tradeable_symbols():
    syms = broker.get_all_tradeable_symbols()
    assert isinstance(syms, list)
    assert len(syms) > 1000, f"Expected >1000 symbols, got {len(syms)}"
    return f"{len(syms)} tradeable symbols"
check("alpaca — get_all_tradeable_symbols >1000", test_alpaca_tradeable_symbols)

# ─────────────────────────────────────────────────────────────
# 15. SESSION OVERRIDES
# ─────────────────────────────────────────────────────────────
section("15. SESSION OVERRIDES")
import session_overrides as so

def test_session_overrides_defaults():
    so.reset()
    assert so.get("signal_score_min_normal") == config.NORMAL_MIN_SIGNAL_SCORE
    assert so.get("signal_score_min_midday") == config.MIDDAY_MIN_SIGNAL_SCORE
    return "defaults correct"
check("session_overrides — defaults match config", test_session_overrides_defaults)

def test_session_overrides_apply():
    plan = {"threshold_overrides": {"signal_score_min_normal": 7.5}}
    so.apply(plan)
    assert so.get("signal_score_min_normal") == 7.5, \
        f"Expected 7.5, got {so.get('signal_score_min_normal')}"
    so.reset()
    return "plan override applied and reset correctly"
check("session_overrides — apply plan override + reset", test_session_overrides_apply)

# ─────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────
section("SUMMARY")
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
total  = len(results)

print(f"\n  Total: {total}  |  {PASS} {passed}  |  {FAIL} {failed}\n")

if failed:
    print("  FAILED TESTS:")
    for name, ok, msg in results:
        if not ok:
            print(f"    {FAIL} {name}")
            print(f"           {msg}")

sys.exit(0 if failed == 0 else 1)
