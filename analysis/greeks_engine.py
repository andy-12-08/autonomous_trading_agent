"""
Black-Scholes Greeks engine for options pricing and position analysis.

Computes delta, gamma, theta, vega, and rho for European-style options.
Used by the strategy selector to choose optimal strikes and monitor live
position Greeks for risk management.

All option prices are per share (multiply by 100 for per-contract values).
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from scipy.stats import norm

import config
from core.database import log


class GreeksEngine:
    """
    Black-Scholes model for option pricing and Greeks computation.

    Supports both calls and puts. All inputs are in standard units:
      - Prices in dollars
      - Volatility as annualized fraction (0.25 = 25%)
      - DTE in calendar days (converted to years internally)
      - Rates as annualized fraction (0.05 = 5%)
    """

    # Minimum DTE to avoid division-by-zero in time-to-expiry calculations
    _MIN_T = 1.0 / 365.0   # 1 calendar day

    @staticmethod
    def compute_greeks(
        spot:        float,
        strike:      float,
        dte:         int,
        iv:          float,
        option_type: str,
        rate:        float = config.RISK_FREE_RATE,
    ) -> dict:
        """
        Compute all Black-Scholes Greeks for a single option.

        Args:
            spot:        Current underlying price.
            strike:      Option strike price.
            dte:         Days to expiration (calendar days).
            iv:          Implied volatility as annualized fraction (e.g. 0.25).
            option_type: 'call' or 'put'.
            rate:        Risk-free rate as annualized fraction (default from config).

        Returns:
            Dict containing:
              price  – theoretical option price (per share)
              delta  – rate of price change per $1 move in underlying
              gamma  – rate of delta change per $1 move in underlying
              theta  – daily time decay in dollars per share (negative = decay)
              vega   – price change per 1% move in IV (per share)
              rho    – price change per 1% move in risk-free rate
              intrinsic  – max(0, S−K) for calls, max(0, K−S) for puts
              time_value – price − intrinsic
              is_call    – True for calls, False for puts
        """
        if spot <= 0 or strike <= 0 or iv <= 0:
            return GreeksEngine._zero_greeks(option_type)

        T = max(dte / 365.0, GreeksEngine._MIN_T)
        is_call = option_type.lower() == "call"

        sqrt_T = math.sqrt(T)
        d1 = (math.log(spot / strike) + (rate + 0.5 * iv ** 2) * T) / (iv * sqrt_T)
        d2 = d1 - iv * sqrt_T

        nd1  = norm.cdf(d1)
        nd2  = norm.cdf(d2)
        nnd1 = norm.cdf(-d1)
        nnd2 = norm.cdf(-d2)
        pdf1 = norm.pdf(d1)

        discount = math.exp(-rate * T)

        if is_call:
            price     = spot * nd1 - strike * discount * nd2
            delta     = nd1
            intrinsic = max(0.0, spot - strike)
            rho       = strike * T * discount * nd2 / 100
        else:
            price     = strike * discount * nnd2 - spot * nnd1
            delta     = nd1 - 1.0   # negative for puts
            intrinsic = max(0.0, strike - spot)
            rho       = -strike * T * discount * nnd2 / 100

        gamma = pdf1 / (spot * iv * sqrt_T)
        theta = (
            -(spot * pdf1 * iv) / (2 * sqrt_T)
            - rate * strike * discount * (nd2 if is_call else nnd2)
        ) / 365.0   # per calendar day

        vega = spot * pdf1 * sqrt_T / 100   # per 1% move in IV

        price     = max(0.0, price)
        time_value = max(0.0, price - intrinsic)

        return {
            "price":      round(price,      4),
            "delta":      round(delta,      4),
            "gamma":      round(gamma,      6),
            "theta":      round(theta,      4),
            "vega":       round(vega,       4),
            "rho":        round(rho,        4),
            "intrinsic":  round(intrinsic,  4),
            "time_value": round(time_value, 4),
            "is_call":    is_call,
        }

    @staticmethod
    def compute_spread_greeks(
        long_leg:    dict,
        short_leg:   dict,
        contracts:   int = 1,
    ) -> dict:
        """
        Compute net Greeks for a two-leg vertical spread position.

        For debit spreads: long leg cost − short leg credit = net debit paid.
        For credit spreads: short leg credit − long leg cost = net credit received.

        The sign convention for a spread Greeks = long_leg − short_leg.

        Args:
            long_leg:  Greeks dict from compute_greeks for the long leg.
            short_leg: Greeks dict from compute_greeks for the short leg.
            contracts: Number of contracts (multiplies by 100 internally).

        Returns:
            Dict with net delta, gamma, theta, vega, net_price, max_profit,
            max_loss, and breakeven for the full position.
        """
        multiplier = contracts * 100

        net_delta = (long_leg["delta"]  - short_leg["delta"])  * multiplier
        net_gamma = (long_leg["gamma"]  - short_leg["gamma"])  * multiplier
        net_theta = (long_leg["theta"]  - short_leg["theta"])  * multiplier
        net_vega  = (long_leg["vega"]   - short_leg["vega"])   * multiplier
        net_price = (long_leg["price"]  - short_leg["price"])  * multiplier   # positive = debit

        # Width of spread (assuming same expiry)
        spread_width = abs(long_leg.get("strike", 0) - short_leg.get("strike", 0))
        spread_value = spread_width * multiplier

        if net_price > 0:
            # Debit spread: max profit = spread_width − net_debit; max loss = net_debit
            max_profit = spread_value - net_price
            max_loss   = net_price
        else:
            # Credit spread: max profit = net_credit; max loss = spread_width − net_credit
            max_profit = abs(net_price)
            max_loss   = spread_value - abs(net_price)

        return {
            "net_delta":    round(net_delta,  4),
            "net_gamma":    round(net_gamma,  6),
            "net_theta":    round(net_theta,  4),
            "net_vega":     round(net_vega,   4),
            "net_price":    round(net_price,  2),
            "max_profit":   round(max_profit, 2),
            "max_loss":     round(max_loss,   2),
            "spread_width": spread_width,
            "is_debit":     net_price > 0,
        }

    @staticmethod
    def compute_iron_condor_greeks(
        put_long:  dict,
        put_short: dict,
        call_short: dict,
        call_long:  dict,
        contracts: int = 1,
    ) -> dict:
        """
        Compute net Greeks for a 4-leg iron condor.

        Iron condor = short put spread + short call spread.
        Net credit = call spread credit + put spread credit.
        Max loss    = wider spread width − net credit.

        Args:
            put_long:   Greeks for the long put (lower strike).
            put_short:  Greeks for the short put.
            call_short: Greeks for the short call.
            call_long:  Greeks for the long call (higher strike).
            contracts:  Number of iron condors.

        Returns:
            Dict with net Greeks and iron condor-specific P&L profile.
        """
        multiplier = contracts * 100

        net_delta = (
            put_long["delta"]  - put_short["delta"]
            + call_short["delta"] - call_long["delta"]  # short call spreads have negative delta from long
        ) * multiplier

        net_theta = (
            put_long["theta"]  - put_short["theta"]
            - call_short["theta"] + call_long["theta"]
        ) * multiplier

        net_vega = (
            put_long["vega"]  - put_short["vega"]
            - call_short["vega"] + call_long["vega"]
        ) * multiplier

        net_gamma = (
            put_long["gamma"]  - put_short["gamma"]
            - call_short["gamma"] + call_long["gamma"]
        ) * multiplier

        # Net credit = sum of premium received from short legs minus long legs
        net_credit = (
            put_short["price"]  - put_long["price"]
            + call_short["price"] - call_long["price"]
        ) * multiplier

        put_spread_width  = abs(put_short.get("strike",  0) - put_long.get("strike",  0))
        call_spread_width = abs(call_long.get("strike",  0) - call_short.get("strike", 0))
        max_loss          = max(put_spread_width, call_spread_width) * multiplier - net_credit

        return {
            "net_delta":   round(net_delta,  4),
            "net_gamma":   round(net_gamma,  6),
            "net_theta":   round(net_theta,  4),  # positive = theta positive (we want this)
            "net_vega":    round(net_vega,   4),  # negative = short vega
            "net_credit":  round(net_credit, 2),
            "max_profit":  round(net_credit, 2),
            "max_loss":    round(max_loss,   2),
            "is_debit":    False,
        }

    @staticmethod
    def select_strike(
        chain_df,
        target_delta:  float,
        option_type:   str,
        spot:          float,
        dte:           int,
        iv_col:        str = "impliedVolatility",
        strike_col:    str = "strike",
    ) -> Optional[dict]:
        """
        Select the strike from a yfinance options chain whose delta is closest
        to target_delta.

        Uses Black-Scholes to compute delta for each row when a delta column
        is not present (yfinance does not return Greeks directly).

        Args:
            chain_df:      DataFrame from ticker.option_chain().calls or .puts.
            target_delta:  Absolute target delta (e.g. 0.30 for OTM).
            option_type:   'call' or 'put'.
            spot:          Current underlying price.
            dte:           Days to expiry.
            iv_col:        Column name for implied volatility.
            strike_col:    Column name for strike price.

        Returns:
            Dict with strike, delta, price (mid), bid, ask, iv, and oi fields,
            or None if the chain is empty or all strikes fail liquidity gates.
        """
        if chain_df is None or chain_df.empty:
            return None

        df = chain_df[chain_df["bid"] > 0].copy()
        df = df[df.get("openInterest", pd.Series([0] * len(df))).fillna(0) >= config.MIN_OPTION_OPEN_INTEREST]

        if df.empty:
            return None

        best_row   = None
        best_delta_dist = float("inf")

        for _, row in df.iterrows():
            strike = float(row[strike_col])
            iv     = float(row.get(iv_col, 0.25))
            if iv < 0.01:
                iv = 0.25
            g = GreeksEngine.compute_greeks(spot, strike, dte, iv, option_type)
            delta_dist = abs(abs(g["delta"]) - target_delta)
            if delta_dist < best_delta_dist:
                best_delta_dist = delta_dist
                bid = float(row.get("bid", 0))
                ask = float(row.get("ask", 0))
                best_row = {
                    "strike":        strike,
                    "delta":         g["delta"],
                    "price":         round((bid + ask) / 2, 2),
                    "bid":           bid,
                    "ask":           ask,
                    "iv":            iv,
                    "oi":            int(row.get("openInterest", 0) or 0),
                    "volume":        int(row.get("volume", 0) or 0),
                    "contract_sym":  str(row.get("contractSymbol", "")),
                    "greeks":        g,
                }

        return best_row

    @staticmethod
    def live_pnl(
        entry_price: float,
        current_price: float,
        contracts: int,
        is_long: bool,
    ) -> float:
        """
        Compute live P&L for an options position.

        Args:
            entry_price:   Premium paid or received at entry (per share).
            current_price: Current mark price (per share).
            contracts:     Number of contracts.
            is_long:       True if we own the option (paid premium).

        Returns:
            Unrealized P&L in dollars (positive = profit).
        """
        multiplier = contracts * 100
        if is_long:
            return (current_price - entry_price) * multiplier
        else:
            return (entry_price - current_price) * multiplier

    @staticmethod
    def _zero_greeks(option_type: str) -> dict:
        """Return a zeroed-out Greeks dict for error cases."""
        return {
            "price": 0.0, "delta": 0.0, "gamma": 0.0,
            "theta": 0.0, "vega":  0.0, "rho":   0.0,
            "intrinsic": 0.0, "time_value": 0.0,
            "is_call": option_type.lower() == "call",
        }


# Import needed for type hint in select_strike
try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore
