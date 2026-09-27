"""Overfitting checks for a fixed-parameter fighter strategy, ported from the other repo's
strategy/robustness.py (origin/claude/vigilant-curie-r6wlfu) with the same tests and thresholds:

1. vs_random_distribution : same params fired at randomly timed entries (same trade count).
2. noise_test             : perturb OHLC, check the (p90-p10)/median spread of edge_score (< 0.5 = robust).
3. permutation_test       : shuffle the bar-to-bar return order; does the real edge beat the shuffled ones?

Only for fixed parameters: a GA strategy would need the whole GA re-run for every variant.
One deliberate change: the noise sigma is 5% of the mean bar range, not 5% of price (their daily-data
setting), because 5% of price would wipe out an H1 forex bar entirely.

    python -m fx_research.robustness --instrument COFFEE.c --fixed short,0.5,2,0.5,4,0,24 --spread 0.31
"""
import argparse

import numpy as np
import pandas as pd

from .backtest import FighterParams, direction_for, simulate_trade
from .config import Settings
from .features import atr
from .metrics import compute_edge
from .scan import BarLoader
from .store import make_supabase_client
from .walkforward import MIN_WARMUP_DAYS, day_close_positions

PERCENTILE_SIGNIFICANT = 95.0
NOISE_SPREAD_ROBUST_THRESHOLD = 0.5


def simulate_fixed(bars, params, spread, days=None, only=None):
    """Enter at each trading-day close when flat (or only at the day indexes in `only`). Returns (R list, times)."""
    o, h, l, c = (bars[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    atr_v, n = atr(bars).to_numpy(), len(bars)
    days = days or day_close_positions(bars)
    rs, times, busy_until = [], [], -1
    idxs = range(MIN_WARMUP_DAYS, len(days)) if only is None else sorted(only)
    for i in idxs:
        p = days[i][1]
        if p <= busy_until:
            continue
        drift = c[p] - c[days[max(i - 7, 0)][1]]
        sign = direction_for(params.direction_mode, drift)
        stop = min(p + 2 + params.ttl_bars + params.max_hold_bars, n)
        busy_until = stop - 1
        r = simulate_trade(o, h, l, c, atr_v, p, stop, sign, params, spread) if stop - p > 2 else None
        if r is not None:
            rs.append(r)
            times.append(bars.index[p])
    return rs, times


def _edge_score(bars, params, spread, days=None, only=None):
    return compute_edge(*simulate_fixed(bars, params, spread, days, only))["edge_score"]


def vs_random(bars, params, spread, n_random=200, seed=0):
    rng = np.random.default_rng(seed)
    days = day_close_positions(bars)
    rs, times = simulate_fixed(bars, params, spread, days)
    real = compute_edge(rs, times)
    n_entries = real["n_trades"]
    pool = np.arange(MIN_WARMUP_DAYS, len(days))
    scores = [_edge_score(bars, params, spread, days, rng.choice(pool, size=min(n_entries, len(pool)), replace=False))
              for _ in range(n_random)]
    rank = float((np.array(scores) < real["edge_score"]).mean() * 100)
    return {"real_edge_score": real["edge_score"], "random_median": round(float(np.median(scores)), 4),
            "random_p90": round(float(np.percentile(scores, 90)), 4),
            "percentile_rank_vs_random": round(rank, 1), "beats_random_at_95pct": rank >= PERCENTILE_SIGNIFICANT}


def noise_test(bars, params, spread, n_variants=100, seed=0):
    rng = np.random.default_rng(seed)
    days = day_close_positions(bars)
    sigma = 0.05 * float(((bars["High"] - bars["Low"]) / bars["Close"]).mean())
    scores = []
    for _ in range(n_variants):
        v = bars.copy()
        for col in ("Open", "High", "Low", "Close"):
            v[col] = bars[col] * (1 + rng.normal(0, sigma, len(bars)))
        v["High"], v["Low"] = v[["Open", "High", "Low", "Close"]].max(axis=1), v[["Open", "High", "Low", "Close"]].min(axis=1)
        scores.append(_edge_score(v, params, spread, days))
    arr = np.array(scores)
    med = float(np.median(arr))
    spread_ratio = (np.percentile(arr, 90) - np.percentile(arr, 10)) / abs(med) if med else float("inf")
    return {"noise_sigma": round(sigma, 6), "median_edge_score": round(med, 4),
            "spread_ratio": round(float(spread_ratio), 3), "robust_spread_lt_0_5": bool(spread_ratio < NOISE_SPREAD_ROBUST_THRESHOLD)}


def permutation_test(bars, params, spread, n_shuffles=100, seed=0):
    rng = np.random.default_rng(seed)
    days = day_close_positions(bars)
    real = _edge_score(bars, params, spread, days)
    close = bars["Close"].to_numpy()
    rets = np.diff(np.log(close))
    avg_rel_range = float(((bars["High"] - bars["Low"]) / bars["Close"]).mean())
    scores = []
    for _ in range(n_shuffles):
        c = np.empty(len(bars))
        c[0] = close[0]
        c[1:] = close[0] * np.exp(np.cumsum(rng.permutation(rets)))
        o = np.concatenate([[c[0]], c[:-1]])
        wick = np.abs(rng.normal(0, avg_rel_range / 4, len(bars))) * c
        v = pd.DataFrame({"Open": o, "High": np.maximum(o, c) + wick, "Low": np.minimum(o, c) - wick,
                          "Close": c, "Volume": bars["Volume"].to_numpy()}, index=bars.index)
        scores.append(_edge_score(v, params, spread, days))
    arr = np.array(scores)
    rank = float((arr < real).mean() * 100)
    return {"real_edge_score": real, "shuffled_median": round(float(np.median(arr)), 4),
            "shuffled_p90": round(float(np.percentile(arr, 90)), 4), "percentile_rank_vs_shuffled": round(rank, 1),
            "depends_on_real_sequence_at_95pct": rank >= PERCENTILE_SIGNIFICANT}


def run_all(bars, params, spread, n_random=200, n_noise=100, n_shuffles=100):
    return {"vs_random_distribution": vs_random(bars, params, spread, n_random),
            "noise_test": noise_test(bars, params, spread, n_noise),
            "permutation_test": permutation_test(bars, params, spread, n_shuffles)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instrument", required=True)
    ap.add_argument("--fixed", required=True, help="direction_mode,k_atr,sl_atr,tp_atr,ttl,reprice,hold")
    ap.add_argument("--spread", type=float, required=True)
    ap.add_argument("--data-source", default="FTMO_MT4_demo")
    ap.add_argument("--run-id", help="store the result in this strategy_runs.summary")
    ap.add_argument("--n", type=int, default=100, help="variants per test (random uses 2x)")
    args = ap.parse_args(argv)
    f = args.fixed.split(",")
    params = FighterParams(f[0], float(f[1]), float(f[2]), float(f[3]), int(f[4]), int(f[5]), int(f[6]))
    s = Settings()
    s.bar_source, s.timeframe, s.history_years, s.prices_source = "supabase", "H1", 1.0, args.data_source
    client = make_supabase_client(s)
    bars = BarLoader(s, client).load(args.instrument)
    rs, times = simulate_fixed(bars, params, args.spread)
    print("real edge:", compute_edge(rs, times))
    res = run_all(bars, params, args.spread, 2 * args.n, args.n, args.n)
    for k, v in res.items():
        print(k, v)
    if args.run_id:
        db = client.schema(s.results_schema).table("strategy_runs")
        summ = db.select("summary").eq("id", args.run_id).execute().data[0]["summary"] or {}
        db.update({"summary": {**summ, "robustness": res}}).eq("id", args.run_id).execute()
        print("stored in run", args.run_id)


if __name__ == "__main__":
    main()
