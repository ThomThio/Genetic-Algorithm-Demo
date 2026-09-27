"""fx_research/backtest.py previously subtracted a flat, mostly-guessed spread (spread_for()'s
static table / JPY heuristic) and modeled zero swap at all. cost_model.py replaces that with a
live-sample-based spread estimate (falling back to exactly spread_for()'s old behavior when
nothing better exists) plus a measured/overridable swap table. These tests pin down: the
fallback is bit-identical to today's behavior (no regression when -- as in this sandbox --
there are never any live samples), the quantile/lookback math is correct, overnight rollover
counting matches the rest of the codebase's own 17:00-New-York convention, and simulate_trade's
refactor from a flat `spread` scalar to a `cost_fn(entry_time, exit_time)` callable still
subtracts exactly the expected cost.
"""
import numpy as np
import pandas as pd
import pytest

from fx_research import cost_model
from fx_research.backtest import FighterParams, flat_cost_fn, simulate_trade, spread_for


# --- spread: fallback + quantile math ---------------------------------------

def test_spread_for_instrument_falls_back_to_static_table_without_client():
    for ins in ('USDSGD', 'COFFEE.c', 'USDJPY', 'EURUSD'):
        assert cost_model.spread_for_instrument(ins, client=None) == spread_for(ins)


def test_estimate_spread_from_samples_none_when_insufficient():
    now = pd.Timestamp('2026-01-10', tz='UTC')
    samples = pd.DataFrame({'ts': [now - pd.Timedelta(hours=1)] * 5, 'spread': [0.0001] * 5})
    assert cost_model.estimate_spread_from_samples(samples, min_samples=30, now=now) is None
    assert cost_model.estimate_spread_from_samples(pd.DataFrame(columns=['ts', 'spread']), now=now) is None


def test_estimate_spread_from_samples_quantile_and_lookback():
    now = pd.Timestamp('2026-01-10', tz='UTC')
    recent_spreads = list(np.linspace(0.0001, 0.0002, 40))   # 40 recent samples, known distribution
    stale_spreads = [10.0] * 40   # way outside the lookback window; must be excluded
    samples = pd.DataFrame({
        'ts': [now - pd.Timedelta(hours=i) for i in range(40)] + [now - pd.Timedelta(days=90)] * 40,
        'spread': recent_spreads + stale_spreads,
    })
    estimate = cost_model.estimate_spread_from_samples(samples, lookback_days=30, min_samples=30,
                                                         quantile=0.75, now=now)
    expected = float(pd.Series(recent_spreads).quantile(0.75))
    assert estimate == pytest.approx(expected)
    assert estimate < 1.0   # sanity: the stale outliers must not have leaked in


# --- overnight rollover counting ---------------------------------------------

def test_overnight_count_zero_within_same_trading_day():
    entry = pd.Timestamp('2026-01-07 10:00', tz='UTC')   # well before 17:00 NY rollover
    exit_ = pd.Timestamp('2026-01-07 18:00', tz='UTC')
    assert cost_model.overnight_count(entry, exit_) == 0


def test_overnight_count_one_rollover():
    entry = pd.Timestamp('2026-01-07 10:00', tz='UTC')
    exit_ = pd.Timestamp('2026-01-08 10:00', tz='UTC')   # crosses one 17:00 NY boundary
    assert cost_model.overnight_count(entry, exit_) == 1


def test_overnight_count_multiple_rollovers():
    entry = pd.Timestamp('2026-01-07 10:00', tz='UTC')
    exit_ = pd.Timestamp('2026-01-10 10:00', tz='UTC')   # three calendar rollovers
    assert cost_model.overnight_count(entry, exit_) == 3


def test_overnight_count_accepts_naive_timestamps():
    # simulate_trade's `times` come from a tz-aware bars.index in practice, but the helper
    # should not blow up if given naive timestamps (treated as UTC).
    assert cost_model.overnight_count('2026-01-07 10:00', '2026-01-08 10:00') == 1


# --- swap: measured/overridable table, zero when unmeasured ------------------

def test_swap_for_defaults_to_zero_for_unmeasured_instrument():
    assert cost_model.swap_for('SOME_UNMEASURED_PAIR') == 0.0


def test_swap_for_env_override(monkeypatch):
    monkeypatch.setenv('FXR_SWAPS', 'USDSGD=0.00004,COFFEE.c=0.02')
    assert cost_model.swap_for('USDSGD') == pytest.approx(0.00004)
    assert cost_model.swap_for('COFFEE.c') == pytest.approx(0.02)
    assert cost_model.swap_for('EURUSD') == 0.0


# --- cost_price_units composition --------------------------------------------

def test_cost_price_units_is_spread_only_with_zero_nights():
    entry = pd.Timestamp('2026-01-07 10:00', tz='UTC')
    exit_ = pd.Timestamp('2026-01-07 12:00', tz='UTC')   # same trading day, no rollover
    cost = cost_model.cost_price_units('USDSGD', entry, exit_, client=None)
    assert cost == pytest.approx(spread_for('USDSGD'))


def test_cost_price_units_adds_swap_per_rollover(monkeypatch):
    monkeypatch.setenv('FXR_SWAPS', 'USDSGD=0.00004')
    entry = pd.Timestamp('2026-01-07 10:00', tz='UTC')
    exit_ = pd.Timestamp('2026-01-09 10:00', tz='UTC')   # two rollovers
    cost = cost_model.cost_price_units('USDSGD', entry, exit_, client=None)
    assert cost == pytest.approx(spread_for('USDSGD') + 2 * 0.00004)


# --- simulate_trade: refactored cost_fn plumbing preserves exact math --------

def _bars(rows, start='2026-01-05 00:00'):   # a Monday, well clear of any weekend edge case
    idx = pd.date_range(start, periods=len(rows), freq='h', tz='UTC')
    o, h, l, c = zip(*rows)
    return np.array(o, dtype=float), np.array(h, dtype=float), np.array(l, dtype=float), \
        np.array(c, dtype=float), idx


def test_simulate_trade_flat_cost_fn_matches_manual_calculation():
    # decision at bar 0 (close=100); limit order 0.5*ATR below/above; fills next bar; hits target.
    o, h, l, c, times = _bars([
        (100, 100, 100, 100),   # bar0: decision
        (99.5, 99.5, 99.3, 99.4),   # bar1: limit (long, dist=0.5) fills at 99.5
        (99.4, 101.0, 99.3, 100.9),  # bar2: target hit (tp = entry + 0.92*atr)
    ])
    atr_v = np.array([1.0, 1.0, 1.0])
    p = FighterParams(direction_mode='long', k_atr=0.5, sl_atr=0.6, tp_atr=0.92, ttl_bars=4,
                       reprice_every=0, max_hold_bars=10)

    r_free = simulate_trade(o, h, l, c, atr_v, times, 0, 3, 1, p, flat_cost_fn(0.0))
    r_costly = simulate_trade(o, h, l, c, atr_v, times, 0, 3, 1, p, flat_cost_fn(0.02))

    assert r_free is not None and r_costly is not None
    risk = p.sl_atr * 1.0
    assert r_costly == pytest.approx(r_free - 0.02 / risk)


def test_simulate_trade_cost_fn_receives_real_entry_and_exit_times():
    """A cost_fn that only returns non-zero when overnight_count(entry, exit) > 0 should return
    the frictionless result for a same-bar-region trade and a strictly worse one once the trade
    is forced to hold across a rollover -- proving cost_fn actually sees real timestamps, not a
    stub, after the spread-scalar-to-callable refactor."""
    o, h, l, c, times = _bars([
        (100, 100, 100, 100),
        (99.5, 99.5, 99.3, 99.4),
        (99.4, 99.6, 99.3, 99.5),
        (99.5, 99.6, 99.3, 99.5),
    ] + [(99.5, 99.6, 99.3, 99.5)] * 30)   # flat bars: forces a time exit well after 24h (1 rollover)
    atr_v = np.array([1.0] * len(o))
    p = FighterParams(direction_mode='long', k_atr=0.5, sl_atr=0.6, tp_atr=5.0, ttl_bars=4,
                       reprice_every=0, max_hold_bars=26)   # tp far away, exits on max_hold (time)

    def cost_fn(entry_time, exit_time):
        return 0.05 if cost_model.overnight_count(entry_time, exit_time) > 0 else 0.0

    r = simulate_trade(o, h, l, c, atr_v, times, 0, len(o), 1, p, cost_fn)
    r_free = simulate_trade(o, h, l, c, atr_v, times, 0, len(o), 1, p, flat_cost_fn(0.0))
    assert r is not None and r_free is not None
    assert r < r_free   # the rollover-aware cost_fn actually fired a non-zero cost
