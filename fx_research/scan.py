"""One scan: for each pair, label the current 7-trading-day regime, find the closest
windows in the last 3 years, and evolve the fighter-entry settings that worked best
after those windows.

    python -m fx_research.scan                       # uses env settings (see config.py)
    python -m fx_research.scan --source csv --csv-dir ../MT4-TradeSignals/currency_data --dry-run
"""
import argparse
import logging
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import data
from .backtest import MT4_DEFAULT, evaluate, fitness, spread_for, direction_for
from .config import REGIMES, Settings
from .features import RegimeThresholds, atr, build_windows, classify, rank_analogs
from .optimizer import run_ga
from .store import DryRunStore, SupabaseStore, make_supabase_client

log = logging.getLogger("fx_research")

VALIDATION_FRACTION = 0.3


class BarLoader:
    def __init__(self, settings, supabase_client=None):
        self.s = settings
        self.client = supabase_client
        self.mt4 = None

    def load(self, instrument):
        s = self.s
        if s.bar_source == "csv":
            bars = data.load_csv(instrument, s.csv_dir)
        elif s.bar_source == "mt4":
            if self.mt4 is None:
                self.mt4 = data.MT4Bars(s)
            bars = self.mt4.load(instrument, s.timeframe, data.bars_needed(s.history_years, s.timeframe))
        elif s.bar_source == "supabase":
            since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(365.25 * s.history_years) + 14)
            bars = data.load_supabase(self.client, instrument, s, since)
        else:
            raise ValueError(f"unknown bar source {s.bar_source}")
        return data.drop_weekends(data.trim_history(bars, s.history_years + 14 / 365.25))

    def close(self):
        if self.mt4 is not None:
            self.mt4.close()


def _iso(ts):
    return pd.Timestamp(ts).isoformat()


def analyse_instrument(instrument, bars, s, run_id):
    """Pure analysis step (no I/O). Returns (snapshot, analogs, strategy) records."""
    min_days = s.window_days * 3 + s.forward_days
    windows, days, first_pos, last_pos = build_windows(bars, s.window_days)
    if len(days) < min_days or len(windows) < 3:
        raise ValueError(f"{instrument}: only {len(days)} trading days of data, need at least {min_days}")

    current, history = windows[-1], windows[:-1]
    th = RegimeThresholds.fit([w.feats for w in history])
    for w in windows:
        w.regime = classify(w.feats, th)

    arrays = (bars["Open"].to_numpy(), bars["High"].to_numpy(), bars["Low"].to_numpy(),
              bars["Close"].to_numpy(), atr(bars).to_numpy())
    close = arrays[3]
    atr_v = arrays[4]

    # Historical windows whose forward period ends before the current window starts (no look-ahead).
    current_first_day = current.end_day - s.window_days + 1
    candidates = [w for w in history if w.end_day + s.forward_days < current_first_day]

    def forward_stop(w):
        return int(last_pos[w.end_day + s.forward_days]) + 1

    # How each regime has behaved over the whole history (next forward_days move, in ATR).
    regime_stats = {}
    for name in REGIMES:
        moves = [(close[forward_stop(w) - 1] - close[w.end_pos]) / atr_v[w.end_pos]
                 for w in candidates if w.regime == name and atr_v[w.end_pos] > 0]
        regime_stats[name] = {
            "count": len(moves),
            "mean_fwd_move_atr": float(np.mean(moves)) if moves else None,
            "up_share": float(np.mean(np.array(moves) > 0)) if moves else None,
        }

    matches = rank_analogs(current, candidates, s.top_k)

    # Decision points: the close of each matched window, then each of the following
    # trading-day closes inside the forward period.
    def cases_for(w):
        stop = forward_stop(w)
        pts = [w.end_pos] + [int(last_pos[w.end_day + j]) for j in range(1, s.forward_days)]
        return [(p, stop, w.feats["drift_z"]) for p in pts if p < stop - 1]

    by_time = sorted(matches, key=lambda m: m[0].end)
    n_val = int(round(len(by_time) * VALIDATION_FRACTION)) if len(by_time) >= 6 else 0
    train = [m[0] for m in by_time[:len(by_time) - n_val]]
    val = [m[0] for m in by_time[len(by_time) - n_val:]] if n_val else []
    val_ids = {id(w) for w in val}
    train_cases = [c for w in train for c in cases_for(w)]
    val_cases = [c for w in val for c in cases_for(w)]
    all_cases = train_cases + val_cases

    spread = spread_for(instrument)
    min_fills = max(3, len(train_cases) // 5)
    ga = run_ga(lambda p: fitness(evaluate(arrays, train_cases, p, spread), min_fills),
                population_size=s.ga_population, generations=s.ga_generations,
                seed=s.ga_seed, seed_params=[MT4_DEFAULT])
    best = ga.params

    def metrics(p, cases):
        return evaluate(arrays, cases, p, spread).to_dict()

    m_train, m_val, m_all = metrics(best, train_cases), metrics(best, val_cases), metrics(best, all_cases)
    base_all = metrics(MT4_DEFAULT, all_cases)
    recommended = bool(m_train["mean_r"] > 0 and m_train["fills"] >= min_fills and
                       (not val_cases or (m_val["mean_r"] > 0 and m_val["fills"] > 0)))

    now = datetime.now(timezone.utc).isoformat()
    as_of = _iso(current.end)
    cur_atr = float(atr_v[current.end_pos])
    direction = direction_for(best.direction_mode, current.feats["drift_z"])

    snapshot = {
        "run_id": run_id, "instrument": instrument, "as_of": as_of,
        "window_start": _iso(current.start), "window_end": as_of, "window_days": s.window_days,
        "regime": current.regime, "features": current.feats, "thresholds": th.to_dict(),
        "regime_history": regime_stats, "history_start": _iso(bars.index[0]),
        "history_windows": len(candidates), "created_at": now,
    }
    analogs = []
    for rank, (w, dist, corr) in enumerate(matches, 1):
        stop = forward_stop(w)
        a0 = atr_v[w.end_pos]
        fwd = close[stop - 1] - close[w.end_pos]
        analogs.append({
            "run_id": run_id, "instrument": instrument, "as_of": as_of, "rank": rank,
            "window_start": _iso(w.start), "window_end": _iso(w.end), "regime": w.regime,
            "same_regime": w.regime == current.regime, "distance": dist, "shape_corr": corr,
            "fwd_return": float(fwd / close[w.end_pos]),
            "fwd_move_atr": float(fwd / a0) if a0 > 0 else None,
            "split": "validate" if id(w) in val_ids else "train", "created_at": now,
        })
    strategy = {
        "run_id": run_id, "instrument": instrument, "as_of": as_of, "regime": current.regime,
        "strategy": "fighter_limit", "params": best.to_dict(),
        "train_metrics": m_train, "validate_metrics": m_val, "all_metrics": m_all,
        "baseline_params": MT4_DEFAULT.to_dict(), "baseline_metrics": base_all,
        "fitness": ga.fitness, "ga": {"generations": ga.generations, "evaluations": ga.evaluations,
                                      "best_by_generation": ga.history},
        "n_analogs": len(matches), "recommended": recommended,
        "live_hint": {
            "direction": "LONG" if direction > 0 else "SHORT",
            "last_close": float(close[current.end_pos]), "atr": cur_atr,
            "entry_distance": best.k_atr * cur_atr, "sl_distance": best.sl_atr * cur_atr,
            "tp_distance": best.tp_atr * cur_atr, "ttl_bars": best.ttl_bars,
            "reprice_every": best.reprice_every,
        },
        "created_at": now,
    }
    return snapshot, analogs, strategy


def run_scan(settings=None, store=None, loader=None):
    s = settings or Settings()
    client = None
    if store is None or loader is None:
        if s.has_supabase:
            client = make_supabase_client(s)
    if store is None:
        store = SupabaseStore(client, s.results_schema) if client else DryRunStore(s.dry_run_dir)
    if loader is None:
        loader = BarLoader(s, client)

    run_id = str(uuid.uuid4())
    store.start_run({
        "id": run_id, "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
        "instruments": s.instruments,
        "settings": {k: getattr(s, k) for k in ("timeframe", "window_days", "history_years", "forward_days",
                                                "top_k", "ga_population", "ga_generations", "bar_source")},
    })
    results, errors = {}, {}
    try:
        for ins in s.instruments:
            try:
                bars = loader.load(ins)
                snap, analogs, strat = analyse_instrument(ins, bars, s, run_id)
                store.save_instrument(snap, analogs, strat)
                results[ins] = strat
                log.info("%s regime=%s params=%s train=%s", ins, snap["regime"], strat["params"],
                         strat["train_metrics"])
            except Exception as e:
                errors[ins] = f"{e}"
                log.error("%s failed: %s", ins, traceback.format_exc())
    finally:
        loader.close()
        status = "ok" if not errors else ("failed" if not results else "partial")
        store.finish_run(run_id, status, "; ".join(f"{k}: {v}" for k, v in errors.items()) or None)
    return run_id, results, errors


def summarize(results):
    lines = []
    for ins, r in results.items():
        t, v = r["train_metrics"], r["validate_metrics"]
        lines.append(f"{ins:8s} {r['regime']:16s} {r['live_hint']['direction']:5s} "
                     f"rec={'Y' if r['recommended'] else 'N'} "
                     f"train {t['fills']}/{t['decisions']} fills {t['mean_r']:+.2f}R  "
                     f"val {v['fills']}/{v['decisions']} {v['mean_r']:+.2f}R  "
                     f"base {r['baseline_metrics']['mean_r']:+.2f}R")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instruments", help="comma-separated, e.g. GBPUSD,AUDUSD")
    ap.add_argument("--source", choices=["supabase", "mt4", "csv"])
    ap.add_argument("--csv-dir")
    ap.add_argument("--dry-run", action="store_true", help="write JSON files instead of Supabase")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    s = Settings()
    if args.instruments:
        s.instruments = [x.strip() for x in args.instruments.split(",")]
    if args.source:
        s.bar_source = args.source
    if args.csv_dir:
        s.csv_dir = args.csv_dir
    if args.seed is not None:
        s.ga_seed = args.seed
    store = DryRunStore(s.dry_run_dir) if args.dry_run else None
    loader = None
    if args.dry_run and s.bar_source == "supabase" and s.has_supabase:
        loader = BarLoader(s, make_supabase_client(s))
    run_id, results, errors = run_scan(s, store=store, loader=loader)
    print(f"run {run_id}")
    print(summarize(results))
    for ins, e in errors.items():
        print(f"{ins:8s} ERROR {e}")
    return 1 if errors and not results else 0


if __name__ == "__main__":
    raise SystemExit(main())
