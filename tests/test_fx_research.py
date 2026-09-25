import numpy as np
import pandas as pd
import pytest

from fx_research import data
from fx_research.backtest import FighterParams, evaluate, simulate_trade
from fx_research.config import Settings
from fx_research.features import RegimeThresholds, build_windows, classify, window_features
from fx_research.optimizer import decode, encode, run_ga
from fx_research.scan import analyse_instrument
from fx_research.scheduler import market_open


def synthetic_bars(days=800, seed=0, drift=0.0, start="2022-01-03 00:00"):
    """H1 bars on a 24h/5d FX calendar (Sunday 22:00 UTC open to Friday 21:00 UTC close)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days * 24, freq="h", tz="UTC")
    idx = idx[data.trading_dates(idx).dayofweek < 5]
    r = rng.normal(drift, 0.001, len(idx))
    close = 1.2 * np.exp(np.cumsum(r))
    open_ = np.concatenate([[1.2], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0004, len(idx))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0004, len(idx))))
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                         "Volume": 100.0}, index=idx)


def bars_from(rows, start="2024-01-02 00:00"):
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx).assign(Volume=1.0)


# --- trading days -----------------------------------------------------------

def test_trading_date_rolls_at_5pm_new_york():
    idx = pd.DatetimeIndex(["2024-01-07 22:00", "2024-01-05 21:30", "2024-01-05 22:30"], tz="UTC")
    d = data.trading_dates(idx)
    assert d[0].day_name() == "Monday"      # Sunday 17:00 NY open belongs to Monday
    assert d[1].day_name() == "Friday"
    assert d[2].day_name() == "Saturday"    # after the Friday close -> weekend


def test_last_trading_days_skips_weekends():
    idx = pd.date_range("2024-01-01", "2024-01-20", freq="h", tz="UTC")
    df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx)
    last = data.last_trading_days(df, 7)
    days = data.trading_dates(last.index).unique()
    assert len(days) == 7
    assert all(d.dayofweek < 5 for d in days)
    # 7 trading days back from Friday 19th spans the previous week's Thursday
    assert days[0] == pd.Timestamp("2024-01-11")


def test_normalize_bars_accepts_mt4_tradesignals_csv_columns():
    raw = pd.DataFrame({"Datetime": ["2024-11-20 20:00:00+08:00"], "Open": [1.0], "High": [1.1],
                        "Low": [0.9], "Close": [1.05], "Volume": [10], "original_tz": ["Asia/Dubai"]})
    df = data.normalize_bars(raw)
    assert list(df.columns) == data.OHLC
    assert df.index[0] == pd.Timestamp("2024-11-20 12:00", tz="UTC")


# --- regimes ------------------------------------------------------------------

def test_classify_trend_and_sharp_trend():
    th = RegimeThresholds()
    up = synthetic_bars(8, seed=1, drift=0.0002)
    feats, _ = window_features(up)
    assert classify(feats, th) in ("Uptrend", "Sharp_Uptrend")
    down = synthetic_bars(8, seed=1, drift=-0.0006)
    feats, _ = window_features(down)
    assert classify(feats, th) == "Sharp_Downtrend"


def test_classify_sideways_for_mean_reverting_series():
    # Ornstein-Uhlenbeck style: price keeps getting pulled back to 1.2
    n, rng = 24 * 7, np.random.default_rng(2)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = 0.3 * x[i - 1] + rng.normal(0, 0.001)
    close = 1.2 + x
    df = pd.DataFrame({"Open": close, "High": close + 1e-4, "Low": close - 1e-4, "Close": close},
                      index=pd.date_range("2024-01-02", periods=n, freq="h", tz="UTC"))
    feats, _ = window_features(df)
    assert classify(feats, RegimeThresholds()) == "Sideways"


def test_windows_are_n_trading_days_long():
    bars = synthetic_bars(40)
    windows, days, *_ = build_windows(bars, 7)
    for w in windows[:5]:
        span = data.trading_dates(bars.loc[w.start:w.end].index).unique()
        assert len(span) == 7


# --- fighter entry simulator --------------------------------------------------

def arrays(df, a=0.001):
    return (df["Open"].to_numpy(), df["High"].to_numpy(), df["Low"].to_numpy(),
            df["Close"].to_numpy(), np.full(len(df), a))


def test_long_limit_fills_then_hits_target():
    # decision close 1.0000, ATR 0.001, limit at 0.9995, TP at +0.001
    df = bars_from([(1.0, 1.0, 1.0, 1.0), (1.0, 1.0002, 0.9994, 0.9996), (0.9996, 1.0010, 0.9996, 1.0008)])
    p = FighterParams("long", k_atr=0.5, sl_atr=1.0, tp_atr=1.0, ttl_bars=5, reprice_every=0, max_hold_bars=10)
    r = simulate_trade(*arrays(df), 0, len(df), 1, p, spread=0.0)
    assert r == pytest.approx(1.0)


def test_limit_never_touched_is_not_a_trade():
    df = bars_from([(1.0, 1.0, 1.0, 1.0)] + [(1.0, 1.001, 0.9999, 1.0)] * 5)
    p = FighterParams("long", k_atr=0.5, sl_atr=1.0, tp_atr=1.0, ttl_bars=3)
    assert simulate_trade(*arrays(df), 0, len(df), 1, p, spread=0.0) is None


def test_fighter_reprice_chases_price_and_fills():
    # price runs up; a static limit 0.5 ATR below never fills, a re-pricing one does
    rows = [(1.0, 1.0, 1.0, 1.0), (1.0, 1.0010, 1.0000, 1.0010), (1.0010, 1.0020, 1.0010, 1.0020),
            (1.0020, 1.0020, 1.0014, 1.0016), (1.0016, 1.0040, 1.0016, 1.0040)]
    df = bars_from(rows)
    static = FighterParams("long", 0.5, 1.0, 1.0, ttl_bars=10, reprice_every=0)
    chase = FighterParams("long", 0.5, 1.0, 1.0, ttl_bars=10, reprice_every=1)
    assert simulate_trade(*arrays(df), 0, len(df), 1, static, 0.0) is None
    assert simulate_trade(*arrays(df), 0, len(df), 1, chase, 0.0) == pytest.approx(1.0)


def test_stop_wins_when_stop_and_target_share_a_bar():
    df = bars_from([(1.0, 1.0, 1.0, 1.0), (1.0, 1.0, 0.9995, 0.9995), (0.9995, 1.01, 0.99, 1.0)])
    p = FighterParams("long", 0.5, 1.0, 1.0, ttl_bars=5)
    assert simulate_trade(*arrays(df), 0, len(df), 1, p, 0.0) == pytest.approx(-1.0)


def test_short_side_mirrors_long():
    df = bars_from([(1.0, 1.0, 1.0, 1.0), (1.0, 1.0006, 0.9998, 1.0004), (1.0004, 1.0004, 0.9990, 0.9992)])
    p = FighterParams("short", 0.5, 1.0, 1.0, ttl_bars=5)
    assert simulate_trade(*arrays(df), 0, len(df), -1, p, 0.0) == pytest.approx(1.0)


def test_spread_reduces_r():
    df = bars_from([(1.0, 1.0, 1.0, 1.0), (1.0, 1.0002, 0.9994, 0.9996), (0.9996, 1.0010, 0.9996, 1.0008)])
    p = FighterParams("long", 0.5, 1.0, 1.0, ttl_bars=5)
    assert simulate_trade(*arrays(df), 0, len(df), 1, p, spread=0.0001) == pytest.approx(0.9)


# --- GA -------------------------------------------------------------------------

def test_encode_decode_roundtrip():
    p = FighterParams("fade", 0.33, 0.6, 0.92, 8, 2, 48)
    q = decode(encode(p))
    assert q.direction_mode == "fade"
    assert q.k_atr == pytest.approx(0.33, abs=0.03)
    assert q.ttl_bars == 8 and q.reprice_every == 2


def test_ga_finds_known_optimum():
    target = FighterParams("short", 0.8, 1.5, 2.0, 12, 3, 30)

    def score(p):
        return -(abs(p.k_atr - target.k_atr) + abs(p.sl_atr - target.sl_atr) + abs(p.tp_atr - target.tp_atr)
                 + abs(p.ttl_bars - target.ttl_bars) / 10 + (p.direction_mode != "short"))

    res = run_ga(score, population_size=40, generations=60, seed=3)
    assert res.params.direction_mode == "short"
    assert res.fitness > -1.0
    assert res.history[-1] >= res.history[0]


# --- end to end -----------------------------------------------------------------

def test_analyse_instrument_three_years_no_lookahead():
    bars = synthetic_bars(365 * 3 + 30, seed=7)
    s = Settings()
    s.ga_population, s.ga_generations, s.ga_seed = 16, 5, 1
    snap, analogs, strat = analyse_instrument("EURUSD", bars, s, "run-1")
    assert snap["regime"] in ("Uptrend", "Sharp_Uptrend", "Downtrend", "Sharp_Downtrend", "Sideways", "Random")
    assert snap["history_windows"] > 700          # ~3 years of daily-stepped windows
    assert len(analogs) == s.top_k
    window_start = pd.Timestamp(snap["window_start"])
    for a in analogs:
        # matched window plus its forward period must finish before the current window starts
        assert pd.Timestamp(a["window_end"]) + pd.Timedelta(days=s.forward_days) < window_start + pd.Timedelta(days=3)
        assert pd.Timestamp(a["window_end"]) < window_start
    assert strat["train_metrics"]["decisions"] > 0
    assert strat["live_hint"]["direction"] in ("LONG", "SHORT")


def test_market_open_hours():
    assert market_open(pd.Timestamp("2024-01-10 12:00", tz="UTC"))          # Wednesday
    assert not market_open(pd.Timestamp("2024-01-13 12:00", tz="UTC"))      # Saturday
    assert not market_open(pd.Timestamp("2024-01-12 22:30", tz="UTC"))      # Friday after 17:00 NY
    assert market_open(pd.Timestamp("2024-01-14 22:30", tz="UTC"))          # Sunday after 17:00 NY
