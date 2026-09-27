"""strategy/backtest.py previously had zero transaction-cost modeling at all -- every trade's
R-multiple was pure (exit-entry)/risk. These tests pin down the cost_bps fix: cost_bps=0.0
(the default) must reproduce the old frictionless numbers exactly, and a non-zero cost_bps
must reduce every trade's R-multiple by exactly the expected amount, so nothing downstream
(edge_score, validated_out_of_sample) can silently ignore it.
"""
import numpy as np
import pandas as pd
import pytest

from strategy.backtest import run_backtest


def bars(rows):
    """rows: list of (open, high, low, close). One bar per hour, enough padding for max_hold."""
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="h", tz="UTC")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def test_zero_cost_bps_matches_frictionless_baseline():
    # signal on bar 0 -> entry at bar 1's open (100); target hit at bar 2 (no stop/target overlap)
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 106, 99, 105), (105, 105, 105, 105)])
    atr = pd.Series([1.0, 1.0, 1.0, 1.0], index=df.index)   # stop_atr_mult=2 -> risk=2, target_R=2 -> target=104
    signal = pd.Series([True, False, False, False], index=df.index)

    trades = run_backtest(df, atr, signal, stop_atr_mult=2.0, target_R=2.0, max_hold_bars=5, cost_bps=0.0)
    assert len(trades) == 1
    # entry=100 (bar1 open), target=104 hit at bar2 (high=106) -> r = (104-100)/2 = 2.0 exactly, no cost
    assert trades[0].r_multiple == pytest.approx(2.0)


def test_nonzero_cost_bps_reduces_r_multiple_by_expected_amount():
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 106, 99, 105), (105, 105, 105, 105)])
    atr = pd.Series([1.0, 1.0, 1.0, 1.0], index=df.index)
    signal = pd.Series([True, False, False, False], index=df.index)

    cost_bps = 5.0   # 5 bps of entry price 100 = 0.05 price units
    trades = run_backtest(df, atr, signal, stop_atr_mult=2.0, target_R=2.0, max_hold_bars=5, cost_bps=cost_bps)
    assert len(trades) == 1
    entry_price, risk = 100.0, 2.0
    expected_cost = entry_price * (cost_bps / 10000.0)
    expected_r = ((104.0 - entry_price) - expected_cost) / risk
    assert trades[0].r_multiple == pytest.approx(expected_r)
    assert trades[0].r_multiple < 2.0   # strictly worse than the frictionless case above


def test_cost_bps_scales_with_entry_price():
    # same setup but entry price 10x higher -> cost in price units should scale with it
    df = bars([(1000, 1000, 1000, 1000), (1000, 1000, 1000, 1000),
               (1000, 1060, 990, 1050), (1050, 1050, 1050, 1050)])
    atr = pd.Series([10.0, 10.0, 10.0, 10.0], index=df.index)
    signal = pd.Series([True, False, False, False], index=df.index)

    trades = run_backtest(df, atr, signal, stop_atr_mult=2.0, target_R=2.0, max_hold_bars=5, cost_bps=5.0)
    assert len(trades) == 1
    expected_cost = 1000.0 * (5.0 / 10000.0)   # 0.5 price units, ten times the previous test's cost
    expected_r = ((1040.0 - 1000.0) - expected_cost) / 20.0
    assert trades[0].r_multiple == pytest.approx(expected_r)
