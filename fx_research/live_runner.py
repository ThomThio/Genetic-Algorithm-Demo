"""Live-ish runner: keeps prices fresh, runs the analysis on each new H1 bar, and publishes everything the
web app (web/) shows to fx_research.live_state: the tick, the current window, the closest historical
windows (analogs, as price paths aligned to now) and the entry/SL/TP plan. Also places fighter orders,
but ONLY when all three hold: started with --allow-live, the instrument's trading_control row is enabled,
and armed_until is in the future. Without --allow-live it is paper: it analyses and logs but never orders.

    python -m fx_research.live_runner --instruments USDSGD:short,COFFEE.c:short             # paper
    python -m fx_research.live_runner --instruments USDSGD:short --allow-live               # can place orders

Needs MT4 open with ZmqCommunicatorEA (MT4_TRADESIGNALS_PATH set) and sql/fx_live_schema.sql applied.
"""
import argparse
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import cost_model
from . import data
from .backfill import upsert_bars
from .config import Settings
from .decision import Decision, decide
from .execute import (ACCOUNT_BALANCE, MAX_BAR_AGE_HOURS, RISK_PCT, FighterOrder, fighter_prices, get_symbol_info,
                      has_our_exposure, size_lots_info)
from .features import atr
from .runlog import RunLogger
from .scan import BarLoader, analyse_instrument
from .scheduler import market_open
from .store import make_supabase_client
from .track_live import mark_open_trades, sync
from .walkforward import day_close_positions

SOURCE = "FTMO_MT4_demo"
COMMAND_TTL_S = 60   # a UI command older than this when the runner sees it is dropped, never executed late
N_ANALOGS = 5
VERSION = "live_runner/1"


def _now():
    return datetime.now(timezone.utc)


def analog_paths(bars, analogs, s, atr_now, last_close):
    """Closest historical windows as price paths (window + the forward period that followed), scaled by ATR so
    they can be drawn over the current price: path[end_idx] sits at the current close."""
    c, atr_v = bars["Close"].to_numpy(), atr(bars).to_numpy()
    days = day_close_positions(bars)
    day_of_pos = {p: i for i, (_, p) in enumerate(days)}
    out = []
    for a in analogs[:N_ANALOGS]:
        end = bars.index.get_loc(pd.Timestamp(a["window_end"]))
        start = bars.index.get_loc(pd.Timestamp(a["window_start"]))
        i = day_of_pos.get(end)
        stop = days[min(i + s.forward_days, len(days) - 1)][1] if i is not None else end
        seg = c[start:stop + 1]
        scaled = (seg - c[end]) / atr_v[end] * atr_now + last_close
        out.append({"rank": a["rank"], "distance": a["distance"], "shape_corr": a["shape_corr"],
                    "regime": a["regime"], "same_regime": a["same_regime"], "split": a["split"],
                    "window_start": a["window_start"], "window_end": a["window_end"],
                    "fwd_move_atr": a["fwd_move_atr"], "end_idx": int(end - start),
                    "times": [int(t.timestamp()) for t in bars.index[start:stop + 1]],
                    "path": [round(float(x), 6) for x in scaled]})
    return out


def plan_from_tick(direction, tick, params, sym, balance, risk):
    """Where the fighter entry would go right now (recomputed on every tick), sized so a stop-out risks `risk`
    of the balance INCLUDING the spread. Stop and target are the GA's ATR multiples (params sl_atr / tp_atr)."""
    anchor = tick["bid"] if direction == "SHORT" else tick["ask"]
    limit, sl, tp = fighter_prices(direction, anchor, params["atr"], tick["ask"], sym, params)
    spread = tick["ask"] - tick["bid"]
    info = size_lots_info(balance, risk, limit, sl, sym, spread)
    sl_dist, tp_dist = abs(sl - limit), abs(tp - limit)
    warnings = []
    if info["capped"]:
        warnings.append("lots capped at the broker max, so real risk is below the target")
    if info["below_min"]:
        warnings.append("risk budget is below the minimum lot")
    if sl_dist and spread / sl_dist > 0.3:
        warnings.append(f"spread is {spread / sl_dist:.0%} of the stop distance")
    return {"direction": direction, "limit": limit, "sl": sl, "tp": tp, "lots": info["lots"],
            "risk_pct": risk, "risk_usd": balance * risk, "risk_usd_actual": info["risk_usd"], "balance": balance,
            "lot_capped": info["capped"], "spread_r": round(spread / sl_dist, 3) if sl_dist else None,
            "ttl_bars": params["ttl_bars"], "reprice_every": params["reprice_every"],
            "rr": round(tp_dist / sl_dist, 2) if sl_dist else None, "warnings": warnings}


SPREAD_SAMPLE_MIN_INTERVAL_S = 60   # throttle: one logged tick per instrument per minute is plenty for a
                                     # trailing-quantile estimate, and keeps spread_samples from growing unbounded


class Instrument:
    def __init__(self, name, direction):
        self.name, self.direction = name, direction.upper()
        self.last_bar = None
        self.params = None
        self.decision_id = None
        self.order = None
        self.sym = None
        self.log = None
        self.analysis = None
        self.last_spread_sample_at = 0.0


def record_spread_sample(fx, instrument, now, tick):
    """Logs one bid/ask spread sample to fx_research.spread_samples (see
    sql/fx_research_spread_samples_schema.sql), the real data cost_model.estimate_spread_from_samples
    reads back to replace backtest.spread_for()'s static guess. Best-effort: the table may not exist
    yet on a fresh install (schema not applied), so a failure here never takes the runner down."""
    try:
        fx("spread_samples").insert({
            "instrument": instrument, "ts": now.isoformat(),
            "bid": tick["bid"], "ask": tick["ask"], "spread": tick["ask"] - tick["bid"],
        }).execute()
    except Exception as e:
        print(f"{instrument}: could not log spread sample ({e!r}); has "
              f"sql/fx_research_spread_samples_schema.sql been applied?")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instruments", required=True, help="NAME:direction,... e.g. USDSGD:short,COFFEE.c:short")
    ap.add_argument("--allow-live", action="store_true", help="allow order placement (still needs the UI switch)")
    ap.add_argument("--tick-seconds", type=float, default=5)
    ap.add_argument("--balance", type=float, default=ACCOUNT_BALANCE)
    ap.add_argument("--risk", type=float, default=RISK_PCT)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    s = Settings()
    s.bar_source, s.timeframe, s.history_years, s.prices_source, s.ga_seed = "supabase", "H1", 1.0, SOURCE, args.seed
    client = make_supabase_client(s)
    fx = lambda t: client.schema(s.results_schema).table(t)
    loader = BarLoader(s, client)
    mode = "live" if args.allow_live else "paper"
    insts = [Instrument(*x.split(":")) for x in args.instruments.split(",")]
    started = _now().isoformat()
    mt4 = data.MT4Bars(s)
    api = mt4.api
    last_sync = 0.0
    print(f"runner up ({mode}). instruments: {[(i.name, i.direction) for i in insts]}")
    try:
        for ins in insts:
            ins.sym = get_symbol_info(api, ins.name)
            ins.log = RunLogger(client, s.results_schema, mode, ins.name,
                                params={"runner": VERSION, "balance": args.balance, "risk_pct": args.risk},
                                direction=ins.direction.lower(), data_source=SOURCE, window_days=s.window_days)
        while True:
            for ins in insts:
                try:
                    step_instrument(s, client, fx, loader, mt4, api, ins, args, mode, started)
                except Exception as e:   # keep the runner alive; surface the error in the UI
                    print(f"{ins.name}: {e!r}")
                    fx("live_state").upsert({"instrument": ins.name, "updated_at": _now().isoformat(),
                                             "runner": {"mode": mode, "error": repr(e), "started_at": started}},
                                            on_conflict="instrument").execute()
            if time.time() - last_sync > 15:
                last_sync = time.time()
                sync(api, client, s.results_schema)
            time.sleep(args.tick_seconds)
    except KeyboardInterrupt:
        print("stopping")
    finally:
        for ins in insts:
            if ins.log:
                ins.log.finish({"stopped": True}, status="stopped")
        mt4.close()
    return 0


def handle_commands(fx, api, ins, tick, armed, args, mode, now):
    """Manual entries requested from the UI (trade_commands). Real orders need --allow-live AND the trading
    switch armed (same gates as automatic entries); otherwise the entry is only SIMULATED: the exact order it
    would send is built and reported back, nothing goes to MT4. Stale commands are dropped, never run late."""
    try:
        cmds = fx("trade_commands").select("*").eq("instrument", ins.name).eq("status", "pending").order("created_at").execute().data
    except Exception:   # table not created yet (sql/fx_trade_commands.sql): manual entries just are not available
        return
    for c in cmds:
        direction = "LONG" if c["action"] == "ENTER_LONG" else "SHORT"
        plan = tick["plans"][direction]
        age = (pd.Timestamp(now) - pd.Timestamp(c["created_at"])).total_seconds()
        live = args.allow_live and armed
        result = {"plan": plan, "mode": "live" if live else "simulated"}
        if age > COMMAND_TTL_S:
            status, result = "expired", {"reason": f"command was {age:.0f}s old when the runner saw it"}
        elif ins.order is not None or (args.allow_live and has_our_exposure(api, ins.name)):
            status, result["reason"] = "rejected", "there is already a working order or open position on this instrument"
        elif plan["lots"] <= 0:
            status, result["reason"] = "rejected", "risk budget is below the minimum lot"
        elif live and not market_open():
            status, result["reason"] = "rejected", "market is closed"
        elif live:
            order = FighterOrder(api, ins.name, direction, ins.params, ins.sym, args.balance, plan["risk_pct"])
            ticket = order.place()
            if ticket:
                ins.order, status, result["ticket"] = order, "placed", ticket
                d = Decision("ENTER", "manual entry (UI button)", {"manual": {"pass": True}}, {"params": ins.params})
                did = ins.log.decision(pd.Timestamp(now), ins.name, d, None, {"plan": plan})
                ins.log.trade(decision_id=did, instrument=ins.name, direction=direction, simulated=False, ticket=ticket,
                              status="pending", placed_at=now.isoformat(), limit_price=order.limit, sl=order.sl,
                              tp=order.tp, lots=order.lots, risk_usd=plan["risk_usd_actual"])
            else:
                status, result["reason"] = "rejected", "MT4 rejected the order"
        else:
            status = "simulated"
            result["reason"] = "runner is in paper mode (no --allow-live)" if not args.allow_live else "trading switch is not armed"
            d = Decision("ENTER", "manual entry (UI button, simulated)", {"manual": {"pass": True}}, {"params": ins.params})
            ins.log.decision(pd.Timestamp(now), ins.name, d, None, {"plan": plan})
        fx("trade_commands").update({"status": status, "result": result, "handled_at": now.isoformat()}).eq("id", c["id"]).execute()
        print(f"{ins.name}: manual {c['action']} -> {status}")


def step_instrument(s, client, fx, loader, mt4, api, ins, args, mode, started):
    now = _now()
    tick_raw = api.Get_last_tick_info(ins.name)
    tick = {"bid": tick_raw["bid"], "ask": tick_raw["ask"], "last": tick_raw.get("last deal price"), "ts": now.isoformat()}

    if time.time() - ins.last_spread_sample_at >= SPREAD_SAMPLE_MIN_INTERVAL_S:
        ins.last_spread_sample_at = time.time()
        record_spread_sample(fx, ins.name, now, tick)

    # 1. new closed H1 bar -> refresh saved prices and re-run the analysis
    fresh = mt4.load(ins.name, "H1", 6)
    closed = fresh[fresh.index + pd.Timedelta(hours=1) <= pd.Timestamp(now)]
    if len(closed) and closed.index[-1] != ins.last_bar:
        ins.last_bar = closed.index[-1]
        tail = data.drop_weekends(mt4.load(ins.name, "H1", 300))
        tail = tail[tail.index + pd.Timedelta(hours=1) <= pd.Timestamp(now)]
        upsert_bars(client, s.prices_schema, s.prices_table, ins.name, "H1", SOURCE, tail)
        ins.sym = get_symbol_info(api, ins.name)   # tick value moves with the account-currency conversion rate
        analyse(s, client, fx, loader, api, ins, args, now)

    mark_open_trades(client, s.results_schema, ins.name, tick, ins.sym)   # floating P&L from this polled tick

    # 2. control row (arming + the fixed risk %), then the plan for both directions
    ctl = fx("trading_control").select("*").eq("instrument", ins.name).execute().data
    risk = float(ctl[0]["risk_pct"]) if ctl else args.risk
    armed = bool(ctl and ctl[0]["enabled"] and ctl[0]["armed_until"] and
                 pd.Timestamp(ctl[0]["armed_until"]) > pd.Timestamp(now))
    state = {"instrument": ins.name, "updated_at": now.isoformat(),
             "runner": {"mode": mode, "allow_live": args.allow_live, "version": VERSION, "started_at": started,
                        "armed": armed, "risk_pct": risk}}
    if ins.params:
        plans = {d: plan_from_tick(d, tick, ins.params, ins.sym, args.balance, risk) for d in ("LONG", "SHORT")}
        tick["plans"], tick["plan"] = plans, plans[ins.direction]
        handle_commands(fx, api, ins, tick, armed, args, mode, now)
    state["tick"] = tick

    # 3. working order, or an automatic entry
    if ins.order is not None:
        result = ins.order.step()
        state["runner"]["order"] = {"ticket": ins.order.ticket, "state": result, "limit": ins.order.limit,
                                    "sl": ins.order.sl, "tp": ins.order.tp, "lots": ins.order.lots}
        if result != "pending":
            ins.order = None
    elif armed and args.allow_live and ins.analysis and ins.analysis["decision"]["action"] == "ENTER" \
            and market_open() and not has_our_exposure(api, ins.name):
        plan = tick["plan"]
        if plan["lots"] > 0:
            order = FighterOrder(api, ins.name, ins.direction, ins.params, ins.sym, args.balance, risk)
            ticket = order.place()
            if ticket:
                ins.order = order
                ins.log.trade(decision_id=ins.decision_id, instrument=ins.name, direction=ins.direction,
                              simulated=False, ticket=ticket, status="pending", placed_at=now.isoformat(),
                              limit_price=order.limit, sl=order.sl, tp=order.tp, lots=order.lots,
                              risk_usd=plan["risk_usd_actual"])
                ins.analysis["decision"] = {**ins.analysis["decision"], "reason": f"order #{ticket} placed"}
    if ins.analysis:
        state["analysis"] = ins.analysis
    fx("live_state").upsert(state, on_conflict="instrument").execute()


def analyse(s, client, fx, loader, api, ins, args, now):
    bars = loader.load(ins.name)
    snap, analogs, strat = analyse_instrument(ins.name, bars, s, "live", direction_modes=[ins.direction.lower()])
    hint = strat["live_hint"]
    last = bars.index[-1]
    age_h = (pd.Timestamp(now) - last).total_seconds() / 3600
    d = decide(strat, bar_age_hours=age_h, max_bar_age_hours=MAX_BAR_AGE_HOURS,
               has_exposure=has_our_exposure(api, ins.name), market_open=market_open())
    ins.params = {**strat["params"], "atr": hint["atr"]}
    win_start = pd.Timestamp(snap["window_start"])
    ins.decision_id = ins.log.decision(last, ins.name, d, snap["regime"],
                                       {"features": snap["features"], "atr": hint["atr"], "bar_age_h": age_h})
    ins.analysis = {
        "asof": last.isoformat(), "bar_age_h": round(age_h, 2), "regime": snap["regime"],
        "features": snap["features"], "direction": ins.direction,
        "window": {"start": snap["window_start"], "end": snap["window_end"], "days": s.window_days,
                   "n_bars": int(((bars.index >= win_start) & (bars.index <= last)).sum())},
        "history_windows": snap["history_windows"], "forward_days": s.forward_days,
        "analogs": analog_paths(bars, analogs, s, hint["atr"], hint["last_close"]),
        "signal": {"recommended": strat["recommended"], "train": strat["train_metrics"],
                   "validate": strat["validate_metrics"], "params": strat["params"], "atr": hint["atr"],
                   "last_close": hint["last_close"]},
        "decision": {"action": d.action, "reason": d.reason, "gates": d.gates},
        "spread": cost_model.spread_for_instrument(ins.name, client=client, schema=s.results_schema),
    }
    print(f"{ins.name}: new bar {last} regime={snap['regime']} -> {d.action} ({d.reason})")


if __name__ == "__main__":
    raise SystemExit(main())
