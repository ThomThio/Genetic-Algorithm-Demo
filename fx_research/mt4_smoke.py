"""Read-only check that the MT4 bridge and the scanner work on the Windows box.

Needs MT4 running with ZmqCommunicatorEA attached (DLL imports allowed) and
MT4_TRADESIGNALS_PATH pointing at the MT4-TradeSignals checkout. Places no orders.

    python -m fx_research.mt4_smoke --instruments GBPUSD,USDJPY
"""
import argparse
import sys

import pandas as pd

from . import data
from .config import Settings
from .features import atr
from .scan import analyse_instrument


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instruments", default="GBPUSD")
    args = ap.parse_args(argv)
    s = Settings()
    s.bar_source = "mt4"
    ok = True

    print("1. Connecting to the EA on port", s.mt4_port)
    mt4 = data.MT4Bars(s)
    try:
        for ins in [x.strip() for x in args.instruments.split(",")]:
            print(f"\n== {ins}")
            tick = mt4.api.Get_last_tick_info(ins)
            print("2. last tick:", tick)
            if not tick:
                ok = False
                continue
            info = mt4.api.Get_instrument_info(ins)
            print("3. instrument info:", info)

            need = data.bars_needed(s.history_years, s.timeframe)
            bars = data.drop_weekends(mt4.load(ins, s.timeframe, need))
            span_days = (bars.index[-1] - bars.index[0]).days if len(bars) else 0
            print(f"4. {len(bars)} H1 bars requested {need}, covering {span_days} days "
                  f"({bars.index[0] if len(bars) else '-'} -> {bars.index[-1] if len(bars) else '-'})")
            if span_days < 365 * s.history_years * 0.9:
                print("   WARNING: less than 3 years. Raise Tools > Options > Charts > "
                      "'Max bars in history' / 'Max bars in chart' and open the H1 chart to download history.")
            print(f"   current H1 ATR(20): {atr(bars).iloc[-1]:.6f}")

            s.ga_generations = 10
            snap, analogs, strat = analyse_instrument(ins, bars, s, "smoke")
            print(f"5. regime {snap['regime']} | {len(analogs)} matches | best {strat['params']}")
            print(f"   train {strat['train_metrics']['mean_r']:+.2f}R ({strat['train_metrics']['fills']} fills), "
                  f"validate {strat['validate_metrics']['mean_r']:+.2f}R, recommended={strat['recommended']}")
            print(f"   live hint: {strat['live_hint']}")
    except Exception as e:
        ok = False
        print("FAILED:", repr(e))
    finally:
        mt4.close()
    print("\nSMOKE TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
