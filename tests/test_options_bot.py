"""
Options bot test suite — no broker connection required.

Covers:
  1. Config constants      — all required constants present and sane
  2. Database              — init, write, read, close for options tables
  3. GreeksEngine          — Black-Scholes price and Greeks math
  4. OCC symbol builder    — format correctness
  5. OptionsRiskManager    — entry gates, sizing, exit rules
  6. IVAnalyzer            — live yfinance IV fetch for SPY (requires internet)
  7. OptionsDecisionEngine — make_decisions() logic (mocked IV data)
  8. DryRun smoke test     — bootstrap stack without broker calls

Run:  python -m pytest tests/test_options_bot.py -v
"""

import sys
import os
import math
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import config
from analysis.greeks_engine import GreeksEngine
from core.options_orders import OptionsOrdersMixin
from risk.options_risk import OptionsRiskManager


# ── 1. Config constants ───────────────────────────────────────────────────────

class TestConfig:
    def test_account_size(self):
        assert config.ACCOUNT_SIZE > 0

    def test_iv_thresholds_ordered(self):
        assert config.IV_RANK_LOW_THRESHOLD < config.IV_RANK_HIGH_THRESHOLD

    def test_dte_aliases_match_source(self):
        assert config.CREDIT_CLOSE_DTE_DAYS == config.CREDIT_CLOSE_AT_DTE
        assert config.DEBIT_CLOSE_DTE_DAYS  == config.DEBIT_CLOSE_AT_DTE

    def test_position_caps(self):
        assert config.MAX_OPEN_OPTIONS_POSITIONS == config.MAX_CONCURRENT_OPTIONS_POSITIONS
        assert config.MAX_OPTIONS_ENTRIES_PER_CYCLE >= 1
        assert config.MAX_CONTRACTS_PER_TRADE >= 1

    def test_exit_rules_sensible(self):
        assert 0 < config.CREDIT_TAKE_PROFIT_PCT < 1.0
        assert config.CREDIT_STOP_LOSS_MULTIPLIER > 1.0
        assert config.CREDIT_CLOSE_DTE_DAYS > config.DEBIT_CLOSE_DTE_DAYS

    def test_vrp_floor_positive(self):
        assert config.MIN_VRP_TO_SELL > 0

    def test_premium_seller_symbols_nonempty(self):
        assert len(config.PREMIUM_SELLER_SYMBOLS) >= 3

    def test_drawdown_limit_fraction(self):
        assert 0 < config.DAILY_DRAWDOWN_LIMIT_PCT < 0.20


# ── 2. Database ───────────────────────────────────────────────────────────────

class TestDatabase:
    @pytest.fixture
    def db(self, tmp_path):
        from core.database import Database
        d = Database(str(tmp_path / "test.db"))
        d.init_db()
        return d

    def _save(self, db, pid, symbol="SPY", strategy="credit_put_spread",
              short_sym="SPY250620P00500000", long_sym="SPY250620P00495000"):
        db.save_options_position(
            position_id=pid, symbol=symbol,
            strategy_type=strategy, contracts=2,
            entry_premium=1.50, max_profit=150.0, max_loss=350.0,
            expiry="2025-06-20", target_dte=30,
            entry_iv_rank=65.0, entry_vrp=3.5,
            net_delta=-5.0, net_theta=8.0, net_vega=-20.0,
            short_symbol=short_sym, long_symbol=long_sym,
        )

    def test_init_creates_tables(self, db):
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "options_positions" in tables
        assert "options_decisions" in tables

    def test_save_and_retrieve_position(self, db):
        pid = str(uuid.uuid4())
        self._save(db, pid)
        pos = db.get_options_position(pid)
        assert pos is not None
        assert pos["symbol"] == "SPY"
        assert pos["contracts"] == 2
        assert abs(pos["entry_premium"] - 1.50) < 0.001

    def test_close_position(self, db):
        pid = str(uuid.uuid4())
        self._save(db, pid, symbol="AAPL", strategy="debit_call_spread",
                   short_sym="AAPL250620C00200000", long_sym="AAPL250620C00195000")
        db.close_options_position(pid, realized_pnl=80.0, close_reason="50pct_profit")
        pos = db.get_options_position(pid)
        assert pos["status"] == "closed"
        assert abs(pos["realized_pnl"] - 80.0) < 0.001

    def test_open_positions_only_returns_open(self, db):
        open_id   = str(uuid.uuid4())
        closed_id = str(uuid.uuid4())
        self._save(db, open_id, symbol="SPY")
        self._save(db, closed_id, symbol="QQQ",
                   short_sym="QQQ250620P00400000", long_sym="QQQ250620P00395000")
        db.close_options_position(closed_id, realized_pnl=-50.0, close_reason="stop_loss")
        open_positions = db.get_open_options_positions()
        symbols = {p["symbol"] for p in open_positions}
        assert "SPY" in symbols
        assert "QQQ" not in symbols

    def test_record_decision(self, db):
        db.record_options_decision(
            symbol="IWM", action="SKIP", strategy_type="credit_put_spread",
            rationale="IV rank too low", iv_rank=28.0, vrp=1.2, signal_score=6.5,
        )
        decisions = db.get_today_options_decisions()
        assert any(d["symbol"] == "IWM" and d["action"] == "SKIP" for d in decisions)


# ── 3. GreeksEngine ───────────────────────────────────────────────────────────

class TestGreeksEngine:

    def test_call_price_positive(self):
        g = GreeksEngine.compute_greeks(spot=100, strike=100, dte=30, iv=0.20, option_type="call")
        assert g["price"] > 0

    def test_put_call_parity(self):
        spot, strike, dte, iv = 100, 100, 91, 0.20
        T    = dte / 365.0
        rate = config.RISK_FREE_RATE
        call = GreeksEngine.compute_greeks(spot, strike, dte, iv, "call")["price"]
        put  = GreeksEngine.compute_greeks(spot, strike, dte, iv, "put")["price"]
        lhs = call - put
        rhs = spot - strike * math.exp(-rate * T)
        assert abs(lhs - rhs) < 0.02, f"Put-call parity violated: {lhs:.4f} vs {rhs:.4f}"

    def test_atm_call_delta_near_half(self):
        g = GreeksEngine.compute_greeks(spot=100, strike=100, dte=30, iv=0.20, option_type="call")
        assert 0.45 <= g["delta"] <= 0.60, f"ATM call delta should be ~0.5, got {g['delta']}"

    def test_put_delta_negative(self):
        g = GreeksEngine.compute_greeks(spot=100, strike=100, dte=30, iv=0.20, option_type="put")
        assert g["delta"] < 0

    def test_theta_negative_for_long_call(self):
        g = GreeksEngine.compute_greeks(spot=100, strike=100, dte=30, iv=0.20, option_type="call")
        assert g["theta"] < 0, "Long call theta must be negative (time decay)"

    def test_vega_positive(self):
        g = GreeksEngine.compute_greeks(spot=100, strike=100, dte=30, iv=0.20, option_type="call")
        assert g["vega"] > 0

    def test_deep_itm_delta_near_one(self):
        g = GreeksEngine.compute_greeks(spot=150, strike=100, dte=30, iv=0.20, option_type="call")
        assert g["delta"] > 0.90

    def test_deep_otm_delta_near_zero(self):
        g = GreeksEngine.compute_greeks(spot=50, strike=100, dte=30, iv=0.20, option_type="call")
        assert g["delta"] < 0.10


# ── 4. OCC Symbol Builder ─────────────────────────────────────────────────────

class TestOCCSymbol:
    def test_call_symbol_format(self):
        sym = OptionsOrdersMixin.build_occ_symbol("AAPL", "2024-12-20", "call", 185.0)
        assert sym == "AAPL241220C00185000"

    def test_put_symbol_contains_right_parts(self):
        sym = OptionsOrdersMixin.build_occ_symbol("SPY", "2025-06-20", "put", 500.0)
        assert "250620" in sym
        assert "P" in sym
        assert "00500000" in sym

    def test_fractional_strike(self):
        sym = OptionsOrdersMixin.build_occ_symbol("MSFT", "2025-03-21", "call", 420.50)
        assert "00420500" in sym

    def test_symbol_length(self):
        sym = OptionsOrdersMixin.build_occ_symbol("AAPL", "2024-12-20", "call", 185.0)
        assert len(sym) <= 21


# ── 5. OptionsRiskManager ─────────────────────────────────────────────────────

class TestOptionsRiskManager:

    def _approve(self, **overrides):
        # max_loss_dollars defaults to $100 — stays under the $150 per-trade cap
        # so individual gate tests isolate only the gate they intend to test.
        defaults = dict(
            symbol="SPY", strategy_type="credit_put_spread",
            max_loss_dollars=100.0,
            daily_premium_at_risk=0.0,
            open_positions_count=1,
            daily_pnl=0.0,
            total_equity=10_000.0,
            portfolio_delta=5.0,
            portfolio_vega=50.0,
            new_position_delta=-5.0,
            new_position_vega=20.0,
            iv_rank=65.0, vrp=3.5,
            signal_score=7.5,
            has_earnings_soon=False,
            consecutive_losses=0,
        )
        defaults.update(overrides)
        return OptionsRiskManager.approve_entry(**defaults)

    def test_clean_entry_approved(self):
        ok, reason = self._approve()
        assert ok, f"Clean entry should be approved: {reason}"

    def test_earnings_blackout_blocks(self):
        ok, reason = self._approve(has_earnings_soon=True)
        assert not ok
        assert "earning" in reason.lower()

    def test_position_cap_blocks(self):
        ok, reason = self._approve(open_positions_count=config.MAX_OPEN_OPTIONS_POSITIONS)
        assert not ok
        assert "position" in reason.lower()

    def test_daily_drawdown_halt(self):
        big_loss = -(config.DAILY_DRAWDOWN_LIMIT + 1)
        ok, reason = self._approve(daily_pnl=big_loss)
        assert not ok

    def test_consecutive_losses_blocks(self):
        ok, reason = self._approve(consecutive_losses=config.MAX_CONSECUTIVE_LOSSES + 1)
        assert not ok

    def test_low_iv_rank_blocks_credit(self):
        ok, reason = self._approve(strategy_type="credit_put_spread", iv_rank=20.0)
        assert not ok

    def test_high_iv_rank_blocks_debit(self):
        ok, reason = self._approve(strategy_type="debit_call_spread", iv_rank=70.0)
        assert not ok

    def test_size_position_at_least_one(self):
        # max_loss_per_contract must fit within MAX_PREMIUM_PER_TRADE ($150)
        n = OptionsRiskManager.size_position(
            strategy_type="credit_put_spread",
            max_loss_per_contract=100.0,    # $100 fits in the $150 budget
            total_equity=10_000.0,
            daily_pnl=0.0,
            vix_level=15.0,
            available_capital=5_000.0,
        )
        assert n >= 1

    def test_size_capped_by_max_contracts(self):
        n = OptionsRiskManager.size_position(
            strategy_type="credit_put_spread",
            max_loss_per_contract=0.01,     # artificially cheap → many contracts
            total_equity=10_000.0,
            daily_pnl=0.0,
            vix_level=15.0,
            available_capital=100_000.0,
        )
        assert n <= config.MAX_CONTRACTS_PER_TRADE

    def test_take_profit_credit(self):
        # Spread worth $0.90 on $2.00 entry = 55% profit taken → trigger at 50%
        ok, reason = OptionsRiskManager.should_take_profit(
            entry_premium=2.00, current_premium=0.90, is_credit=True
        )
        assert ok, f"55% profit on credit should trigger: {reason}"

    def test_no_take_profit_too_early(self):
        ok, _ = OptionsRiskManager.should_take_profit(
            entry_premium=2.00, current_premium=1.50, is_credit=True
        )
        assert not ok

    def test_stop_loss_credit(self):
        # Stop at 2× credit received: entry=2.00, stop loss when current_premium-entry >= 4.00
        # current_premium must be > 2.00 + 4.00 = 6.00
        ok, reason = OptionsRiskManager.should_stop_loss(
            entry_premium=2.00, current_premium=6.50, is_credit=True
        )
        assert ok, f"Spread at $6.50 (2.25× credit above entry) should stop: {reason}"

    def test_dte_exit_credit(self):
        ok, _ = OptionsRiskManager.should_exit_by_dte(dte_remaining=5, strategy_type="credit_put_spread")
        assert ok
        ok2, _ = OptionsRiskManager.should_exit_by_dte(dte_remaining=10, strategy_type="credit_put_spread")
        assert not ok2

    def test_dte_exit_debit(self):
        ok, _ = OptionsRiskManager.should_exit_by_dte(dte_remaining=2, strategy_type="debit_call_spread")
        assert ok

    def test_delta_exit_triggered(self):
        ok, _ = OptionsRiskManager.should_exit_by_delta(short_leg_delta_abs=0.80, strategy_type="credit_put_spread")
        assert ok

    def test_delta_exit_not_triggered(self):
        ok, _ = OptionsRiskManager.should_exit_by_delta(short_leg_delta_abs=0.40, strategy_type="credit_put_spread")
        assert not ok

    def test_max_loss_spread(self):
        loss = OptionsRiskManager.compute_max_loss_spread(spread_width=5.0, net_premium=1.50, contracts=2)
        assert abs(loss - 700.0) < 0.01   # (5 - 1.5) × 100 × 2 = $700

    def test_max_loss_iron_condor(self):
        loss = OptionsRiskManager.compute_max_loss_iron_condor(spread_width=5.0, net_credit=2.00, contracts=1)
        assert abs(loss - 300.0) < 0.01   # (5 - 2) × 100 = $300


# ── 6. IVAnalyzer (live — requires internet) ──────────────────────────────────

@pytest.mark.network
class TestIVAnalyzer:
    @pytest.fixture
    def analyzer(self):
        from data.iv_analyzer import IVAnalyzer
        return IVAnalyzer()

    def test_spy_iv_data_has_all_keys(self, analyzer):
        data = analyzer.get_iv_data("SPY")
        assert data, "SPY IV data should not be empty"
        for key in ("atm_iv", "iv_rank", "vrp", "iv_regime", "expiry"):
            assert key in data, f"Missing key: {key}"

    def test_spy_atm_iv_reasonable(self, analyzer):
        data = analyzer.get_iv_data("SPY")
        if data:
            assert 0.05 <= data["atm_iv"] <= 1.50, f"ATM IV out of range: {data['atm_iv']}"

    def test_iv_regime_valid_value(self, analyzer):
        data = analyzer.get_iv_data("SPY")
        if data:
            assert data["iv_regime"] in ("high", "neutral", "low")

    def test_get_available_expirations_nonempty(self, analyzer):
        exps = analyzer.get_available_expirations("SPY")
        assert len(exps) >= 4, "SPY should have many expiry dates"
        assert exps == sorted(exps)

    def test_expirations_are_future_dates(self, analyzer):
        from datetime import date
        exps = analyzer.get_available_expirations("SPY")
        today = date.today().isoformat()
        assert all(e >= today for e in exps)


# ── 7. OptionsDecisionEngine (mocked) ────────────────────────────────────────

class TestOptionsDecisionEngine:
    @pytest.fixture
    def engine(self):
        from algo.algo_decisions import OptionsDecisionEngine
        return OptionsDecisionEngine()

    def _candidate(self, symbol="SPY", score=7.5, price=500.0):
        return {
            "symbol":       symbol,
            "signal_score": score,
            "indicators":   {"rsi": 50, "atr": 2.5, "vol_ratio": 1.8, "price": price},
            "flow":         {},
            "dark_pool":    {},
            "pre_market":   {},
            "news":         [],
        }

    def _iv_data(self, iv_rank=65.0, vrp=4.0, regime="high"):
        return {
            "atm_iv":        0.18,
            "iv_rank":       iv_rank,
            "iv_percentile": 70.0,
            "realized_vol":  0.14,
            "vrp":           vrp,
            "iv_regime":     regime,
            "expiry":        "2025-06-20",
            "bid_ask_ok":    True,
        }

    def test_enter_on_high_iv(self, engine):
        candidates = [self._candidate("SPY", price=500.0)]
        iv_map = {"SPY": self._iv_data(iv_rank=65.0, vrp=4.0, regime="high")}
        decisions = engine.make_decisions(
            candidates=candidates, open_positions=[],
            iv_data_map=iv_map, market_regime="ranging",
            spy_move_pct=0.1, vix_level=18.0, hour=10, minute=0,
        )
        enters = [d for d in decisions if d["action"] == "ENTER"]
        assert len(enters) >= 1
        assert enters[0]["strategy_type"] in ("credit_put_spread", "credit_call_spread", "iron_condor")

    def test_enter_on_low_iv_with_signal(self, engine):
        candidates = [self._candidate("AAPL", score=8.5, price=185.0)]
        iv_map = {"AAPL": self._iv_data(iv_rank=20.0, vrp=-1.0, regime="low")}
        decisions = engine.make_decisions(
            candidates=candidates, open_positions=[],
            iv_data_map=iv_map, market_regime="trending",
            spy_move_pct=0.5, vix_level=14.0, hour=10, minute=0,
        )
        enters = [d for d in decisions if d["action"] == "ENTER"]
        assert len(enters) >= 1
        assert enters[0]["strategy_type"] in ("debit_call_spread", "debit_put_spread")

    def test_skip_on_neutral_iv(self, engine):
        candidates = [self._candidate("MSFT", price=400.0)]
        iv_map = {"MSFT": self._iv_data(iv_rank=40.0, vrp=1.0, regime="neutral")}
        decisions = engine.make_decisions(
            candidates=candidates, open_positions=[],
            iv_data_map=iv_map, market_regime="ranging",
            spy_move_pct=0.1, vix_level=18.0, hour=10, minute=0,
        )
        skips = [d for d in decisions if d["action"] == "SKIP"]
        assert len(skips) >= 1

    def test_skip_on_earnings(self, engine):
        cand = self._candidate("NVDA", price=900.0)
        cand["earnings_soon"] = True
        iv_map = {"NVDA": self._iv_data(iv_rank=75.0, vrp=6.0, regime="high")}
        decisions = engine.make_decisions(
            candidates=[cand], open_positions=[],
            iv_data_map=iv_map, market_regime="ranging",
            spy_move_pct=0.1, vix_level=18.0, hour=10, minute=0,
        )
        enters = [d for d in decisions if d["action"] == "ENTER"]
        assert len(enters) == 0, "Should not enter during earnings blackout"

    def test_position_cap_respected(self, engine):
        candidates = [self._candidate(f"SYM{i}", price=100.0) for i in range(5)]
        iv_map = {c["symbol"]: self._iv_data() for c in candidates}
        # open_positions is a list of dicts (as returned by database)
        fake_positions = [{"symbol": f"SYM{i}"} for i in range(config.MAX_OPEN_OPTIONS_POSITIONS)]
        decisions = engine.make_decisions(
            candidates=candidates, open_positions=fake_positions,
            iv_data_map=iv_map, market_regime="ranging",
            spy_move_pct=0.1, vix_level=18.0, hour=10, minute=0,
        )
        enters = [d for d in decisions if d["action"] == "ENTER"]
        assert len(enters) == 0, "No entries allowed when at position cap"


# ── 8. Bootstrap smoke test ───────────────────────────────────────────────────

class TestBootstrap:
    def test_build_stack_returns_orchestrator(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        import bootstrap

        mock_broker = MagicMock()
        mock_broker.get_account.return_value = MagicMock(equity=10000, cash=10000)
        mock_broker.is_market_open.return_value = False

        with patch("bootstrap.AlpacaBroker", return_value=mock_broker):
            orch = bootstrap.build_trading_stack(dry_run=True)

        from trading.orchestrator import TradingOrchestrator
        assert isinstance(orch, TradingOrchestrator)
        assert orch._dry_run is True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-m", "not network"])
