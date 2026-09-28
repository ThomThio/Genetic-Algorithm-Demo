"""Periodic (default: every 15 minutes, see scheduler.py) entry watcher for
one instrument -- USDSGD by default. Wraps execute.scan_and_execute() with:

  - the same live/armed gating live_runner.py already uses (fx_research.
    trading_control: --allow-live on this process AND the instrument's row
    has enabled=true AND armed_until in the future), so there is exactly
    one remote kill switch across both live-trading entry points in this
    repo, not two different safety models;
  - Telegram notifications via scheduler.notify (signal found, then
    filled/expired/not-armed), reusing the existing sender as-is.

    python -m fx_research.entry_watch                      # one paper pass on USDSGD
    python -m fx_research.entry_watch --allow-live          # one pass, places a real order if armed

Needs MT4 open with ZmqCommunicatorEA (MT4_TRADESIGNALS_PATH set), same as
execute.py/mt4_smoke.py/live_runner.py.
"""
import argparse
import logging

import pandas as pd

from . import data
from .config import Settings
from .execute import ACCOUNT_BALANCE, RISK_PCT, scan_and_execute
from .scheduler import notify
from .store import make_supabase_client

log = logging.getLogger("fx_research.entry_watch")

DEFAULT_INSTRUMENT = "USDSGD"


def _armed(client, schema, instrument, now=None):
    """Same check live_runner.py uses: enabled=true and armed_until in the future."""
    if client is None:
        return False, RISK_PCT
    ctl = client.schema(schema).table("trading_control").select("*").eq("instrument", instrument).execute().data
    if not ctl:
        return False, RISK_PCT
    row = ctl[0]
    armed = bool(row["enabled"] and row["armed_until"] and
                 pd.Timestamp(row["armed_until"]) > pd.Timestamp(now or pd.Timestamp.now(tz="UTC")))
    return armed, float(row.get("risk_pct", RISK_PCT))


def entry_watch(instrument=DEFAULT_INSTRUMENT, settings=None, allow_live=False, balance=ACCOUNT_BALANCE) -> dict:
    """One pass: connect to MT4, check the arm switch, scan+decide+(maybe)
    execute a fighter entry for `instrument`, notify on Telegram, and
    disconnect. Safe to call every 15 minutes -- decide()'s own
    no_open_exposure gate already prevents placing a second order while an
    earlier one (managed by its own background thread, see
    execute.scan_and_execute) is still pending.
    """
    s = settings or Settings()
    client = make_supabase_client(s) if s.has_supabase else None
    armed, risk_pct = _armed(client, s.results_schema, instrument) if client else (False, RISK_PCT)
    live = bool(allow_live and armed)

    mt4 = data.MT4Bars(s)
    try:
        result = scan_and_execute(mt4, instrument, s, balance=balance, risk_pct=risk_pct,
                                  direction_modes=None, live=live, notify=notify,
                                  run_label="entry_watch_15min")
    finally:
        mt4.close()

    log.info("%s: action=%s allow_live=%s armed=%s live=%s", instrument, result.get("action"),
              allow_live, armed, live)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instrument", default=DEFAULT_INSTRUMENT)
    ap.add_argument("--allow-live", action="store_true",
                     help="permit real orders when fx_research.trading_control is armed for this "
                          "instrument (default: paper -- analyse, decide, and notify, never order)")
    ap.add_argument("--balance", type=float, default=ACCOUNT_BALANCE)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    entry_watch(instrument=args.instrument, allow_live=args.allow_live, balance=args.balance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
