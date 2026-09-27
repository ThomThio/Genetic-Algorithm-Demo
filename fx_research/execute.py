"""Turn the scanner's shorts/longs signal into a fighter-entry limit order in MT4.

Reuses MT4-TradeSignals' EA bridge (EACommunicator_API.Open_order / Get_all_orders /
Change_settings_for_pending_order / Delete_order_by_ticket), same calls as its
tradeHelper.place_limit_order, but with the scanner's GA-tuned distances and risk-based lot sizing.

DRY RUN by default: prints the order it would send and exits. Pass --live to actually send.

    python -m fx_research.execute --instrument USDSGD --direction short            # dry run
    python -m fx_research.execute --instrument USDSGD --direction short --live     # sends the order
"""
import argparse
import json
import math
import os
import sys
import time

import pandas as pd

from . import data
from .config import Settings
from .decision import decide
from .runlog import RunLogger
from .scan import BarLoader, analyse_instrument
from .store import make_supabase_client

BAR_SECONDS = 3600
ACCOUNT_BALANCE = float(os.environ.get("FXR_ACCOUNT_BALANCE", "100000"))
RISK_PCT = float(os.environ.get("FXR_RISK_PCT", "0.03"))
MAX_BAR_AGE_HOURS = float(os.environ.get("FXR_MAX_BAR_AGE_HOURS", "3"))
MAGIC = int(os.environ.get("FXR_MAGIC", "20260926"))
TAG = "fxr"


def size_lots_info(balance, risk_pct, entry, stop, sym, spread=0.0):
    """Lots so that being stopped out loses ~risk_pct of balance, and what that really risks.

    A stop fills on the far side of the spread (a short's stop trades at the ask, a long's at the bid), so a
    stopped-out trade loses the stop distance PLUS the spread: that is what the risk is sized on. Lots round
    DOWN to the lot step; 0 if even the minimum lot would risk more than allowed (never rounds up past the
    budget). `capped` = the broker's max lot cut the size, so the real risk is below the target."""
    risk_dist = abs(entry - stop) + spread
    tick_size, tick_value = float(sym["tickSize"]), float(sym["tickValue"])
    info = {"lots": 0.0, "risk_usd": 0.0, "capped": False, "below_min": False}
    if risk_dist <= 0 or tick_size <= 0 or tick_value <= 0:
        return info
    loss_per_lot = risk_dist / tick_size * tick_value
    step, lo, hi = float(sym["lotStep"]), float(sym["minLotSize"]), float(sym["maxLotSize"])
    lots = math.floor(balance * risk_pct / loss_per_lot / step + 1e-9) * step
    info["capped"] = lots > hi
    lots = min(lots, hi)
    if lots < lo:
        info["below_min"] = True
        return info
    info["lots"] = round(lots, 8)
    info["risk_usd"] = round(lots * loss_per_lot, 2)
    return info


def size_lots(balance, risk_pct, entry, stop, sym, spread=0.0):
    return size_lots_info(balance, risk_pct, entry, stop, sym, spread)["lots"]


def get_symbol_info(api, symbol):
    """GET_SYMBOL_INFO directly: EACommunicator_API.Get_instrument_info KeyErrors on symbols
    missing from its lookup table (e.g. USDSGD)."""
    from EACommunicator_API import TradingCommands
    return json.loads(api.send_command(TradingCommands.GET_SYMBOL_INFO, symbol))


def fighter_prices(direction, anchor, atr, ask, sym, hint):
    """Limit/SL/TP for a fighter entry. Short: sell limit above the market; long: buy limit below."""
    digits = int(sym["digits"])
    point = float(sym["point"])
    floor = float(sym.get("stopLevel", 0)) * point
    if direction == "SHORT":
        limit = max(anchor + hint["k_atr"] * atr, ask + floor)
        sl, tp = limit + hint["sl_atr"] * atr, limit - hint["tp_atr"] * atr
    else:
        limit = min(anchor - hint["k_atr"] * atr, ask - floor)
        sl, tp = limit - hint["sl_atr"] * atr, limit + hint["tp_atr"] * atr
    if floor:   # the broker rejects a stop or target closer than its stop level to the order price
        if direction == "SHORT":
            sl, tp = max(sl, limit + floor), min(tp, limit - floor)
        else:
            sl, tp = min(sl, limit - floor), max(tp, limit + floor)
    return round(limit, digits), round(sl, digits), round(tp, digits)


def _tickets(df):
    return set() if df is None or len(df) == 0 or "ticket" not in df else set(df["ticket"].astype(int))


def order_state(api, ticket):
    if ticket in _tickets(api.Get_all_orders()):
        return "pending"
    if ticket in _tickets(api.Get_all_open_positions()):
        return "filled"
    return "gone"


def has_our_exposure(api, symbol):
    for df in (api.Get_all_orders(), api.Get_all_open_positions()):
        if df is None or len(df) == 0:
            continue
        sym_col = "instrument" if "instrument" in df else "symbol"
        mine = df[(df[sym_col] == symbol) & (df["comment"].astype(str).str.startswith(TAG))]
        if len(mine):
            return True
    return False


class FighterOrder:
    """One working fighter entry: place(), then call step() repeatedly. It re-prices every `reprice_every`
    bars and cancels after `ttl_bars`, like TradeCoreV2.run_fighterEntry / backtest.simulate_trade."""

    def __init__(self, api, symbol, direction, params, sym, balance, risk_pct):
        self.api, self.symbol, self.direction, self.params = api, symbol, direction, params
        self.sym, self.balance, self.risk_pct = sym, balance, risk_pct
        self.ticket = self.lots = self.limit = self.sl = self.tp = None
        self.start = self.last_reprice = None

    def _prices(self):
        tick = self.api.Get_last_tick_info(self.symbol)
        anchor = tick["bid"] if self.direction == "SHORT" else tick["ask"]
        limit, sl, tp = fighter_prices(self.direction, anchor, self.params["atr"], tick["ask"], self.sym, self.params)
        return limit, sl, tp, tick["ask"] - tick["bid"]

    def place(self):
        self.limit, self.sl, self.tp, spread = self._prices()
        self.lots = size_lots(self.balance, self.risk_pct, self.limit, self.sl, self.sym, spread)
        if self.lots <= 0:
            print("Risk budget is smaller than the minimum lot, skipping.")
            return None
        otype = "sell" if self.direction == "SHORT" else "buy"
        ticket = self.api.Open_order(self.symbol, otype, volume=self.lots, openprice=self.limit, slippage=3,
                                     magicnumber=MAGIC, stoploss=self.sl, takeprofit=self.tp,
                                     comment=f"{TAG}:{self.direction}")
        if ticket is None or ticket < 0:
            print("Order rejected by MT4.")
            return None
        self.ticket, self.start = ticket, time.time()
        self.last_reprice = self.start
        print(f"Placed {otype} limit #{ticket}: {self.lots} lots @ {self.limit} SL {self.sl} TP {self.tp}")
        return ticket

    def step(self):
        """Advance the order. Returns 'pending' while working, else 'filled' / 'gone' / 'cancelled' (final)."""
        state = order_state(self.api, self.ticket)
        if state != "pending":
            return state
        if (time.time() - self.start) / BAR_SECONDS >= self.params["ttl_bars"]:
            self.api.Delete_order_by_ticket(self.ticket)
            print(f"Order #{self.ticket} unfilled after {self.params['ttl_bars']} bar(s), cancelled.")
            return "cancelled"
        every = self.params["reprice_every"]
        if every and (time.time() - self.last_reprice) >= every * BAR_SECONDS:
            self.limit, self.sl, self.tp, _ = self._prices()
            self.api.Change_settings_for_pending_order(ticket=self.ticket, price=self.limit,
                                                       stoploss=self.sl, takeprofit=self.tp)
            self.last_reprice = time.time()
            print(f"Re-priced #{self.ticket}: limit {self.limit} SL {self.sl} TP {self.tp}")
        return "pending"


def run_fighter(api, symbol, direction, params, sym, balance, risk_pct):
    """Blocking version for the one-shot CLI: place, then step every 30s until the order is done."""
    order = FighterOrder(api, symbol, direction, params, sym, balance, risk_pct)
    if order.place() is None:
        return None
    while True:
        time.sleep(30)
        state = order.step()
        if state != "pending":
            print(f"Order #{order.ticket} is {state}.")
            return order.ticket


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instrument", required=True)
    ap.add_argument("--direction", choices=["short", "long"], required=True)
    ap.add_argument("--live", action="store_true", help="actually send the order (default: dry run)")
    ap.add_argument("--balance", type=float, default=ACCOUNT_BALANCE)
    ap.add_argument("--risk", type=float, default=RISK_PCT, help="fraction of balance risked per trade")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    s = Settings()
    s.bar_source = "mt4"
    s.ga_seed = args.seed
    mt4 = data.MT4Bars(s)
    try:
        api, symbol = mt4.api, args.instrument
        loader = BarLoader(s, make_supabase_client(s) if s.has_supabase else None)
        loader.mt4 = mt4
        bars = loader.load(symbol)
        age_h = (pd.Timestamp.now(tz="UTC") - bars.index[-1]).total_seconds() / 3600
        snap, _, strat = analyse_instrument(symbol, bars, s, "exec", direction_modes=[args.direction])
        p, hint = strat["params"], strat["live_hint"]
        print(f"{symbol} regime={snap['regime']} recommended={strat['recommended']} "
              f"train={strat['train_metrics']['mean_r']:+.2f}R val={strat['validate_metrics']['mean_r']:+.2f}R "
              f"last bar {age_h:.1f}h old")
        d = decide(strat, bar_age_hours=age_h, max_bar_age_hours=MAX_BAR_AGE_HOURS,
                   has_exposure=has_our_exposure(api, symbol))
        log = None
        if s.has_supabase:
            log = RunLogger(make_supabase_client(s), s.results_schema, "live" if args.live else "paper", symbol,
                            params={**p, "balance": args.balance, "risk_pct": args.risk},
                            direction=args.direction, data_source=s.prices_source or None,
                            window_days=s.window_days)
        inputs = {"features": snap["features"], "atr": hint["atr"], "bar_age_h": age_h}
        if d.action != "ENTER":
            print(f"{d.action}: {d.reason}. No order.")
            if log:
                log.decision(bars.index[-1], symbol, d, snap["regime"], inputs)
                log.finish({"decision": d.action, "reason": d.reason})
            return 0

        sym = get_symbol_info(api, symbol)
        params = {**p, "atr": hint["atr"]}
        tick = api.Get_last_tick_info(symbol)
        direction = args.direction.upper()
        anchor = tick["bid"] if direction == "SHORT" else tick["ask"]
        limit, sl, tp = fighter_prices(direction, anchor, hint["atr"], tick["ask"], sym, params)
        lots = size_lots(args.balance, args.risk, limit, sl, sym, tick["ask"] - tick["bid"])
        risk_usd = args.balance * args.risk
        print(f"Setup: {direction} limit {limit}  SL {sl}  TP {tp}  lots {lots}  "
              f"(risking ${risk_usd:,.0f} = {args.risk:.0%} of ${args.balance:,.0f}; "
              f"ttl {p['ttl_bars']} bar(s), re-price every {p['reprice_every']})")
        order = {"direction": direction, "limit": limit, "sl": sl, "tp": tp, "lots": lots,
                 "risk_usd": risk_usd, "balance_used": args.balance}
        did = log.decision(bars.index[-1], symbol, d, snap["regime"], inputs, order) if log else None
        if not args.live:
            print("DRY RUN: nothing sent. Re-run with --live to place it.")
            if log:
                log.finish({"decision": "ENTER", "dry_run": True})
            return 0
        ticket = run_fighter(api, symbol, direction, params, sym, args.balance, args.risk)
        if log:
            log.trade(decision_id=did, instrument=symbol, direction=direction, simulated=False, ticket=ticket,
                      status="pending" if ticket else "cancelled", placed_at=pd.Timestamp.now(tz="UTC").isoformat(),
                      limit_price=limit, sl=sl, tp=tp, lots=lots,
                      risk_usd=risk_usd)
            log.finish({"decision": "ENTER", "ticket": ticket})
    finally:
        mt4.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
