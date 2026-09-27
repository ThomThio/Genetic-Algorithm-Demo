"""BuildAlpha-style backtest metrics, ported from origin/claude/vigilant-curie-r6wlfu
strategy/fitness.py (compute_edge) with identical definitions and thresholds, so numbers are
comparable across both repos. Every mode (backtest / walk-forward / paper / live) funnels its
realised R multiples through compute_edge().

    python -m fx_research.metrics            # re-score every logged run from its trades
"""
import math

import numpy as np
import pandas as pd

MIN_TRADES_FOR_SIGNAL = 30   # statistical-significance floor (house standard)
OOS_MIN_TRADES = 10          # out-of-sample floor


def _wilson_lower_bound(wins, n, z=1.959963985):
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _span_years(times):
    ts = [pd.Timestamp(t) for t in times if t is not None]
    if len(ts) < 2:
        return None
    days = (max(ts) - min(ts)).total_seconds() / 86400.0
    return days / 365.25 if days > 0 else None


def compute_edge(r_values, times=None):
    """r_values: per-trade R multiples (after spread). times: one timestamp per trade (for the Sharpe
    annualisation). Same fields and rounding as the other repo's EdgeStats."""
    n = len(r_values)
    if n == 0:
        return {"n_trades": 0, "win_rate": 0.0, "win_rate_lb95": 0.0, "avg_win_R": 0.0, "avg_loss_R": 0.0,
                "reward_risk": 0.0, "profit_factor": 0.0, "expectancy_R": 0.0, "edge_score": -10.0,
                "net_profit_R": 0.0, "max_drawdown_R": 0.0, "pnl_to_dd_ratio": 0.0, "sharpe_ratio": None,
                "meets_min_trade_count": False}
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r <= 0]
    win_rate = len(wins) / n
    lb = _wilson_lower_bound(len(wins), n)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    reward_risk = (avg_win / avg_loss) if avg_loss > 0 else float(avg_win)
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    pf = 999.0 if pf == float("inf") else pf
    edge_score = lb * avg_win - (1 - lb) * avg_loss

    cum = np.cumsum(r_values)
    max_dd = float((np.maximum.accumulate(cum) - cum).max())
    net = float(cum[-1])
    pnl_dd = net / max_dd if max_dd > 0 else (999.0 if net > 0 else 0.0)

    sharpe = None
    std = float(np.std(r_values, ddof=1)) if n > 1 else 0.0
    years = _span_years(times) if times else None
    if std > 0 and years:
        sharpe = float(np.mean(r_values)) / std * math.sqrt(n / years)

    return {"n_trades": n, "win_rate": round(win_rate, 4), "win_rate_lb95": round(lb, 4),
            "avg_win_R": round(avg_win, 4), "avg_loss_R": round(avg_loss, 4),
            "reward_risk": round(reward_risk, 4), "profit_factor": round(pf, 4),
            "expectancy_R": round(sum(r_values) / n, 4), "edge_score": round(edge_score, 4),
            "net_profit_R": round(net, 4), "max_drawdown_R": round(max_dd, 4),
            "pnl_to_dd_ratio": round(pnl_dd, 4),
            "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
            "meets_min_trade_count": n >= MIN_TRADES_FOR_SIGNAL}


def rescore_logged_runs():
    """Compute the full metric set for every logged run from its closed trades and store it as the
    run's final run_metrics row (extra.edge)."""
    from .config import Settings
    from .store import make_supabase_client
    s = Settings()
    db = make_supabase_client(s).schema(s.results_schema)
    for run in db.table("strategy_runs").select("id,mode,instrument,direction,params").execute().data:
        rows = (db.table("trades").select("r_multiple,placed_at").eq("run_id", run["id"])
                .eq("status", "closed").order("placed_at").execute().data)
        if not rows:
            continue
        edge = compute_edge([r["r_multiple"] for r in rows], [r["placed_at"] for r in rows])
        fixed = (run["params"] or {}).get("fixed")
        label = f"{run['mode']:11s} {run['instrument']:8s} {(fixed or {}).get('direction_mode') or run['direction']:6s}"
        print(f"{label} n={edge['n_trades']:3d} win={edge['win_rate']:.0%} (lb95 {edge['win_rate_lb95']:.0%}) "
              f"PF={edge['profit_factor']:.2f} exp={edge['expectancy_R']:+.2f}R edge={edge['edge_score']:+.2f} "
              f"P&L/DD={edge['pnl_to_dd_ratio']:.2f} sharpe={edge['sharpe_ratio']} "
              f"{'OK-N' if edge['meets_min_trade_count'] else 'n<30'}  {run['id'][:8]}")
        db.table("run_metrics").upsert(
            {"run_id": run["id"], "as_of": rows[-1]["placed_at"], "final": True, "fills": edge["n_trades"],
             "closed": edge["n_trades"], "mean_r": edge["expectancy_R"], "total_r": edge["net_profit_R"],
             "win_rate": edge["win_rate"], "max_dd_r": edge["max_drawdown_R"], "extra": {"edge": edge}},
            on_conflict="run_id,as_of").execute()


if __name__ == "__main__":
    rescore_logged_runs()
