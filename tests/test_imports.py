"""Full validation — no orders placed."""
import config
import risk_manager as rm
import bucket_manager as bm
import gfv_tracker as gfv
import signal_scorer as scorer
from expectancy import (compute_expectancy, get_recent_consecutive_losses,
                        check_revenge_trade_guard, expectancy_report)
from logger import init_db

init_db()
gfv.init_gfv_db()
print("DB init OK")

# ── signal_scorer ─────────────────────────────────────────────────────────────
good_sig = {
    "price": 185.0, "ema9": 186.0, "ema21": 184.0, "ema50": 182.0,
    "macd_hist": 0.3, "macd_cross": "bullish", "rsi": 52,
    "vol_ratio": 1.8, "atr": 1.85, "above_vwap": True, "mom10": 0.8,
    "vwap": 184.0,
}
score, ev = scorer.score_setup(good_sig)
cls       = scorer.classify(score)
print(f"Good setup score={score} class={cls}  top ev: {ev[:3]}")
assert score >= 6.5, f"Good setup should score ≥6.5, got {score}"

bad_sig = {
    "price": 185.0, "ema9": 183.0, "ema21": 185.0, "ema50": 187.0,
    "macd_hist": -0.5, "macd_cross": "bearish", "rsi": 72,
    "vol_ratio": 0.6, "atr": 1.85, "above_vwap": False, "mom10": -1.5,
    "vwap": 186.0,
}
bad_score, _ = scorer.score_setup(bad_sig)
print(f"Bad setup score={bad_score} class={scorer.classify(bad_score)}")
assert bad_score < 4.0, f"Bad setup should score < 4, got {bad_score}"

# filter_watchlist: bad sig should be dropped
items = [
    {"symbol": "AAPL", "indicators": good_sig},
    {"symbol": "TSLA", "indicators": bad_sig},
]
passed = scorer.filter_watchlist(items, midday=False)
assert any(i["symbol"] == "AAPL" for i in passed), "AAPL should pass"
assert not any(i["symbol"] == "TSLA" for i in passed), "TSLA should be filtered"
assert passed[0].get("signal_score") is not None
print(f"filter_watchlist: {len(passed)}/2 passed. signal_evidence present: {bool(passed[0]['signal_evidence'])}")

# midday filter: raises the bar
midday_passed = scorer.filter_watchlist(items, midday=True)
print(f"Midday filter: {len(midday_passed)}/2 passed (bar=7.5)")

# ── volatility regime ─────────────────────────────────────────────────────────
f_normal,  l1 = rm.volatility_size_factor(atr=1.5, price=185)     # 0.8% → normal
f_elevated, l2 = rm.volatility_size_factor(atr=3.0, price=185)    # 1.6% → elevated
f_high,     l3 = rm.volatility_size_factor(atr=5.5, price=185)    # 3.0% → high
f_extreme,  l4 = rm.volatility_size_factor(atr=8.0, price=185)    # 4.3% → extreme
print(f"Vol regimes: normal={f_normal}({l1}) elevated={f_elevated}({l2}) "
      f"high={f_high}({l3}) extreme={f_extreme}({l4})")
assert f_normal == 1.00
assert f_elevated < 1.0
assert f_high < f_elevated
assert f_extreme < f_high

# is_too_volatile: ATR > 5% should block
assert rm.is_too_volatile(atr=10.0, price=185)    # 5.4%
assert not rm.is_too_volatile(atr=3.0, price=185) # 1.6%
print("is_too_volatile: OK")

# ── calc_qty: ATR reduces qty in high-vol ─────────────────────────────────────
qty_normal  = rm.calc_qty(185, 181.3, 4000, 0, 10000, atr=1.5)
qty_extreme = rm.calc_qty(185, 181.3, 4000, 0, 10000, atr=8.0)
assert qty_extreme <= qty_normal, f"Extreme vol should reduce qty: {qty_extreme} vs {qty_normal}"
print(f"ATR-adjusted sizing: normal_qty={qty_normal}  extreme_qty={qty_extreme}")

# ── exposure cap now 30% ──────────────────────────────────────────────────────
assert config.MAX_TOTAL_EXPOSURE_PCT == 0.30
ok_exp, _ = rm.approve_buy("AAPL", 185, 5, 181,
    settled_cash=4000, deployed_today=2900, num_positions=1,
    daily_pnl=0, total_equity=10000, trades_today=0,
    reward_to_risk=2.5, signal_confidence=8, vol_ratio=1.5, rsi=52)
assert not ok_exp, "2900 + 925 > 3000 (30%) should be vetoed"
print(f"30% exposure cap: correctly vetoed at $2900 deployed")

# ── expectancy tracker ────────────────────────────────────────────────────────
fake_decisions = [
    {"action": "SELL", "pnl":  80},
    {"action": "SELL", "pnl":  60},
    {"action": "SELL", "pnl": -30},
    {"action": "SELL", "pnl":  90},
    {"action": "SELL", "pnl": -25},
    {"action": "SELL", "pnl":  70},
    {"action": "SELL", "pnl": -40},
    {"action": "SELL", "pnl":  55},
    {"action": "SELL", "pnl":  65},
    {"action": "SELL", "pnl": -20},
]
exp = compute_expectancy(fake_decisions)
print(f"Expectancy: {exp['expectancy']:.2f} | WR={exp['win_rate']:.0%} "
      f"avgW=${exp['avg_win']:.0f} avgL=${exp['avg_loss']:.0f} positive={exp['is_positive']}")
assert exp["is_positive"]
print(f"Expectancy report: {expectancy_report(fake_decisions)}")

# ── consecutive loss guard ────────────────────────────────────────────────────
losing_run = [
    {"action": "SELL", "pnl": -20},
    {"action": "SELL", "pnl": -30},
    {"action": "SELL", "pnl": -15},
]
consec = get_recent_consecutive_losses(losing_run)
assert consec == 3, f"Should be 3 consecutive losses, got {consec}"

ok_rtg, r_rtg = check_revenge_trade_guard(3, signal_confidence=7)
assert not ok_rtg, f"3 losses + conf=7 should be blocked: {r_rtg}"

ok_rtg2, r_rtg2 = check_revenge_trade_guard(3, signal_confidence=9)
assert ok_rtg2, f"3 losses + conf=9 should be allowed: {r_rtg2}"

ok_rtg3, _ = check_revenge_trade_guard(0, signal_confidence=7)
assert ok_rtg3, "No losses should always pass"

print(f"Revenge-trade guard: 3 losses conf=7 → blocked | conf=9 → allowed")

# ── volume window check ───────────────────────────────────────────────────────
from trader import is_high_volume_window
assert is_high_volume_window(9, 45),   "9:45 should be high-volume"
assert is_high_volume_window(10, 30),  "10:30 should be high-volume"
assert not is_high_volume_window(12, 0), "12:00 should be midday (low-vol)"
assert is_high_volume_window(14, 45),  "14:45 should be high-volume"
assert not is_high_volume_window(13, 0), "13:00 should be midday"
print("High-volume window checks: OK")

print("\nAll validation checks passed ✓")
