"""Keeps live/paper trades in the fx_research.trades table in sync with MT4 and refreshes each run's
metrics as trades close, so live runs get the same BuildAlpha-style metric set as backtests.

For every non-simulated trade still pending/filled it looks the ticket up in MT4 (pending orders, open
positions, closed history) and updates: status, fill/close time and price, realised R (price move / the
stop distance, so spread and slippage are already in it), USD P&L, exit reason.

    python -m fx_research.track_live                    # sync + refresh metrics (read-only on MT4)
    python -m fx_research.track_live --enforce-max-hold # also close positions held longer than the run's max_hold_bars

The backtest exits at max_hold_bars; live only has SL/TP unless --enforce-max-hold is used.
"""
import argparse
import os

import pandas as pd

from . import data
from .config import Settings
from .runlog import refresh_metrics
from .store import make_supabase_client

BROKER_TZ = os.environ.get("MT4_BROKER_TZ", "Asia/Dubai")


def _col(df, *names):
    return next((n for n in names if n in df.columns), None)


def _find(df, ticket):
    if df is None or len(df) == 0 or "ticket" not in df.columns:
        return None
    m = df[df["ticket"].astype(int) == int(ticket)]
    return m.iloc[-1] if len(m) else None


def _utc(raw):
    try:
        t = pd.Timestamp(str(raw).replace(".", "-", 2))
        return (t.tz_localize(BROKER_TZ) if t.tzinfo is None else t).tz_convert("UTC").isoformat()
    except Exception:
        return None


def resolve_trade(trade, pending_df, open_df, closed_df):
    """Pure function: the update to apply to one live trade row given MT4's current state, or None."""
    ticket = trade["ticket"]
    sign = 1 if trade["direction"] == "LONG" else -1
    if _find(pending_df, ticket) is not None:
        return None
    pos = _find(closed_df, ticket)
    if pos is not None:
        op = float(pos[_col(closed_df, "open_price", "openprice")])
        cl = float(pos[_col(closed_df, "close_price", "closeprice")])
        risk = abs(op - float(trade["sl"])) if trade.get("sl") else None
        r = (cl - op) * sign / risk if risk else None
        tol = 0.1 * (risk or 0)
        reason = ("tp" if trade.get("tp") and abs(cl - float(trade["tp"])) <= tol else
                  "sl" if trade.get("sl") and abs(cl - float(trade["sl"])) <= tol else "manual")
        extras = sum(float(pos[c]) for c in ("swap", "commission") if c in closed_df.columns)
        return {"status": "closed", "entry": op, "exit": cl, "r_multiple": r, "exit_reason": reason,
                "pnl_usd": float(pos["profit"]) + extras,
                "filled_at": _utc(pos[_col(closed_df, "open_time", "opentime")]),
                "closed_at": _utc(pos[_col(closed_df, "close_time", "closetime")])}
    pos = _find(open_df, ticket)
    if pos is not None:
        if trade["status"] == "filled":
            return None
        return {"status": "filled", "entry": float(pos[_col(open_df, "open_price", "openprice")]),
                "filled_at": _utc(pos[_col(open_df, "open_time", "opentime")])}
    # not pending, open or closed: an unfilled order that was cancelled/expired
    return {"status": "cancelled"} if trade["status"] == "pending" else None


def sync(api, client, schema, enforce_max_hold=False):
    """Update every pending/filled live trade from MT4's state, then refresh the affected runs' metrics.
    Returns the number of trades changed."""
    db = lambda: client.schema(schema)
    live = db().table("trades").select("*").eq("simulated", False).in_("status", ["pending", "filled"]).execute().data
    if not live:
        return 0
    pending, opened, closed = api.Get_all_orders(), api.Get_all_open_positions(), api.Get_all_closed_positions()
    touched, changed = set(), 0
    for t in live:
        if t["ticket"] is None:
            continue
        upd = resolve_trade(t, pending, opened, closed)
        if upd is None and enforce_max_hold and t["status"] == "filled" and t.get("filled_at"):
            run = db().table("strategy_runs").select("params").eq("id", t["run_id"]).execute().data[0]
            hold_h = float((run["params"] or {}).get("max_hold_bars", 0))
            age_h = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(t["filled_at"])).total_seconds() / 3600
            if hold_h and age_h >= hold_h:
                print(f"#{t['ticket']} held {age_h:.1f}h >= max hold {hold_h:.0f}h, closing")
                api.Close_position_by_ticket(int(t["ticket"]))
                closed = api.Get_all_closed_positions()
                upd = resolve_trade(t, pending, opened, closed)
        if upd:
            db().table("trades").update(upd).eq("id", t["id"]).execute()
            touched.add(t["run_id"])
            changed += 1
            print(f"#{t['ticket']} -> {upd['status']}" + (f" R={upd['r_multiple']:+.2f}" if upd.get("r_multiple") is not None else ""))
    for run_id in touched:
        edge = refresh_metrics(client, schema, run_id)
        print(f"run {run_id[:8]}: n={edge['n_trades']} exp={edge['expectancy_R']:+.2f}R PF={edge['profit_factor']:.2f}")
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--enforce-max-hold", action="store_true")
    args = ap.parse_args(argv)
    s = Settings()
    client = make_supabase_client(s)
    mt4 = data.MT4Bars(s)
    try:
        if not sync(mt4.api, client, s.results_schema, args.enforce_max_hold):
            print("No live trades changed.")
    finally:
        mt4.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
