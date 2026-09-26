"""Walk-forward (and single-fit backtest) of the scanner + fighter entry, logged to the decision-log tables.

Walk-forward: at the close of each trading day in the test period, re-run the whole analysis
(regime match + GA) using ONLY bars up to that moment, decide with decision.decide(), and if it says
ENTER simulate the trade on the bars that follow. No look-ahead, and the same decision function
the live executor uses.

    python -m fx_research.walkforward --instrument USDSGD --direction short --mode walkforward
    python -m fx_research.walkforward --instrument USDSGD --direction short --mode backtest
"""
import argparse
import sys

import numpy as np
import pandas as pd

from . import data
from .backtest import FighterParams, direction_for, simulate_trade, spread_for
from .config import Settings
from .decision import decide
from .features import atr
from .metrics import compute_edge
from .runlog import RunLogger
from .scan import BarLoader, analyse_instrument
from .store import make_supabase_client

MIN_WARMUP_DAYS = 30
BALANCE, RISK = 100_000.0, 0.03


def day_close_positions(bars):
    dates = data.trading_dates(bars.index)
    last = pd.Series(np.arange(len(bars)), index=dates).groupby(level=0).last()
    return [(pd.Timestamp(d), int(p)) for d, p in last.items() if pd.Timestamp(d).dayofweek < 5]


def summarize_r(rs, balance0, balance):
    if not rs:
        return {"closed": 0, "mean_r": 0.0, "total_r": 0.0, "win_rate": 0.0, "max_dd_r": 0.0,
                "balance": balance, "return_pct": 0.0}
    a = np.array(rs)
    eq = np.concatenate([[0], np.cumsum(a)])
    return {"closed": len(rs), "mean_r": float(a.mean()), "total_r": float(a.sum()), "win_rate": float((a > 0).mean()),
            "max_dd_r": float((np.maximum.accumulate(eq) - eq).max()), "balance": balance,
            "return_pct": (balance / balance0 - 1) * 100}


def run_walkforward(log, ins, bars, s, direction, spread, balance0=BALANCE, risk=RISK, step=1, fixed=None):
    o, h, l, c = (bars[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    atr_v = atr(bars).to_numpy()
    days = day_close_positions(bars)
    balance, rs, times, exposure_until, n = balance0, [], [], -1, len(bars)
    counts = {"decisions": 0, "entries": 0, "fills": 0}
    for day, p in days[MIN_WARMUP_DAYS::step]:
        as_of = bars.index[p]
        try:
            snap, _, strat = analyse_instrument(ins, bars.iloc[:p + 1], s, log.run_id, direction_modes=[direction])
        except ValueError:
            continue
        counts["decisions"] += 1
        d = decide(strat, has_exposure=p <= exposure_until, require_fit=fixed is None)
        params = fixed or FighterParams(**strat["params"])
        hint = strat["live_hint"]
        sign = direction_for(params.direction_mode, snap["features"]["drift_z"])
        limit = c[p] - sign * params.k_atr * atr_v[p]
        sl, tp = limit - sign * params.sl_atr * atr_v[p], limit + sign * params.tp_atr * atr_v[p]
        order = {"direction": "SHORT" if sign < 0 else "LONG", "limit": limit, "sl": sl, "tp": tp,
                 "risk_usd": balance * risk, "balance_used": balance} if d.action == "ENTER" else None
        did = log.decision(as_of, ins, d, snap["regime"], {"features": snap["features"], "atr": hint["atr"],
                                                            "last_close": hint["last_close"]}, order)
        if d.action == "ENTER":
            counts["entries"] += 1
            stop = min(p + 2 + params.ttl_bars + params.max_hold_bars, n)
            r = simulate_trade(o, h, l, c, atr_v, p, stop, sign, params, spread) if stop - p > 2 else None
            exposure_until = stop - 1
            if r is None:
                log.trade(decision_id=did, instrument=ins, direction=order["direction"], simulated=True,
                          status="cancelled", placed_at=str(as_of), limit_price=limit, sl=sl, tp=tp,
                          risk_usd=order["risk_usd"], exit_reason="end_of_data" if stop - p <= 2 else None)
            else:
                counts["fills"] += 1
                pnl = r * balance * risk
                balance += pnl
                rs.append(r)
                times.append(as_of)
                log.trade(decision_id=did, instrument=ins, direction=order["direction"], simulated=True,
                          status="closed", placed_at=str(as_of), limit_price=limit, sl=sl, tp=tp,
                          risk_usd=order["risk_usd"], spread_cost=spread, r_multiple=r, pnl_usd=pnl)
        m = summarize_r(rs, balance0, balance)
        log.metrics(as_of, decisions=counts["decisions"], entries=counts["entries"], fills=counts["fills"],
                    closed=m["closed"], mean_r=m["mean_r"], total_r=m["total_r"], win_rate=m["win_rate"],
                    max_dd_r=m["max_dd_r"], balance=balance, equity=balance)
    summary = {**counts, **summarize_r(rs, balance0, balance)}
    return summary


def log_fit_run(client, schema, ins, snap, strat, trace, direction, mode="backtest", data_source=None,
                window_days=None, spread=None):
    """Log one in-sample fit as a run: the decision, every simulated trade, and the full metric set for all,
    in-sample (train) and out-of-sample (validate) trades."""
    log = RunLogger(client, schema, mode, ins, params={**strat["params"], "spread": spread},
                    direction=direction, data_source=data_source, window_days=window_days)
    d = decide(strat)
    did = log.decision(snap["as_of"], ins, d, snap["regime"], {"features": snap["features"]}, strat["live_hint"])
    sign = "SHORT" if strat["params"]["direction_mode"] == "short" else "LONG"
    for t, r in trace["all"]:
        log.trade(decision_id=did, instrument=ins, direction=sign, simulated=True, status="closed",
                  placed_at=str(t), r_multiple=r, spread_cost=spread)
    tr = trace
    log.finish({"recommended": strat["recommended"], "train": strat["train_metrics"],
                "validate": strat["validate_metrics"], "all": strat["all_metrics"]},
               edges={"in_sample": compute_edge([r for _, r in tr["train"]], [t for t, _ in tr["train"]]),
                      "out_of_sample": compute_edge([r for _, r in tr["validate"]], [t for t, _ in tr["validate"]])})
    return log


def run_backtest(client, s, ins, bars, direction, spread, data_source):
    trace = {}
    snap, _, strat = analyse_instrument(ins, bars, s, "bt", direction_modes=[direction], trace=trace)
    log = log_fit_run(client, s.results_schema, ins, snap, strat, trace, direction, "backtest", data_source,
                      s.window_days, spread)
    return log, {"recommended": strat["recommended"], "train": strat["train_metrics"],
                 "validate": strat["validate_metrics"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instrument", required=True)
    ap.add_argument("--direction", choices=["short", "long"], required=True)
    ap.add_argument("--mode", choices=["walkforward", "backtest"], default="walkforward")
    ap.add_argument("--data-source", default="FTMO_MT4_demo")
    ap.add_argument("--spread", type=float, help="price units; default is a rough per-pair estimate")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--fixed", help="no GA: direction_mode,k_atr,sl_atr,tp_atr,ttl,reprice,hold e.g. fade,0.5,2,0.5,4,0,24")
    ap.add_argument("--step", type=int, default=1, help="decide every N trading days")
    args = ap.parse_args(argv)

    s = Settings()
    s.bar_source, s.timeframe, s.history_years = "supabase", "H1", 1.0
    s.prices_source, s.ga_seed = args.data_source, args.seed
    fixed = None
    if args.fixed:
        f = args.fixed.split(",")
        fixed = FighterParams(f[0], float(f[1]), float(f[2]), float(f[3]), int(f[4]), int(f[5]), int(f[6]))
        s.ga_population, s.ga_generations = 4, 1   # GA output is unused; only regime/features are needed
    client = make_supabase_client(s)
    ins = args.instrument
    bars = BarLoader(s, client).load(ins)
    spread = args.spread if args.spread is not None else spread_for(ins)
    if args.mode == "backtest":
        log, summary = run_backtest(client, s, ins, bars, args.direction, spread, args.data_source)
        print(f"run {log.run_id} (backtest)")
        return 0
    log = RunLogger(client, s.results_schema, args.mode, ins,
                    params={"ga_population": s.ga_population, "ga_generations": s.ga_generations, "seed": s.ga_seed,
                            "spread": spread, "balance": BALANCE, "risk_pct": RISK, "step": args.step,
                            "fixed": fixed.to_dict() if fixed else None},
                    direction=args.direction, data_source=args.data_source, window_days=s.window_days,
                    train_start=str(bars.index[0]), test_start=str(bars.index[0]), test_end=str(bars.index[-1]))
    try:
        summary = run_walkforward(log, ins, bars, s, args.direction, spread, step=args.step, fixed=fixed)
        summary["edge"] = log.finish(summary)
    except Exception as e:
        log.finish({"error": repr(e)}, status="failed")
        raise
    print(f"run {log.run_id} ({args.mode})")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
