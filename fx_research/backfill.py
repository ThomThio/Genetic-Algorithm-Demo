"""One-off backfill: pull N years of H1 bars for an instrument straight from MT4
(via the EA bridge, like mt4_smoke.py), derive any coarser timeframe (e.g. H4) by
resampling in pandas, and upsert into this project's public.fx_prices
(see sql/fx_prices_schema.sql).

H1 is the only timeframe pulled from MT4: the EA bridge (ZmqCommunicatorEA.ex4,
compiled, no source available) ignores the requested timeframe and always returns
H1-spaced bars - confirmed by comparing raw 'H1' vs 'H4' responses for the same
instrument. Anything coarser than H1 is derived from it via data.resample_ohlc.

Needs MT4 running with ZmqCommunicatorEA attached (DLL imports allowed),
MT4_TRADESIGNALS_PATH pointing at the MT4-TradeSignals checkout, and enough
broker H1 history downloaded locally (Tools > Options > Charts > "Max bars in
history" raised, then scroll the H1 chart back so MT4 fetches it).

    python -m fx_research.backfill --instruments USDSGD --timeframes H1,H4 --years 5 --source FTMO_MT4_demo
"""
import argparse
import sys

from . import data
from .config import Settings
from .store import make_supabase_client

BATCH = 500
RESAMPLE_RULE = {"H4": "4h", "D1": "1D"}


def upsert_bars(client, schema, table, instrument, timeframe, source, bars):
    if bars.empty:
        return 0
    df = bars.reset_index().rename(columns={"index": "Datetime"})
    df["Ccy"] = instrument
    df["Timeframe"] = timeframe
    df["Source"] = source
    df["Datetime"] = df["Datetime"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
    records = df[["Ccy", "Timeframe", "Source", "Datetime", "Open", "High", "Low", "Close", "Volume"]].to_dict("records")
    db = client.schema(schema).table(table)
    n = 0
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        db.upsert(chunk, on_conflict="Ccy,Timeframe,Source,Datetime").execute()
        n += len(chunk)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instruments", required=True, help="comma-separated, e.g. USDSGD")
    ap.add_argument("--timeframes", default="H1,H4")
    ap.add_argument("--years", type=float, default=5)
    ap.add_argument("--source", required=True,
                     help="provenance tag, e.g. FTMO_MT4_demo, FTMO_MT4_live, OANDA_MT4_demo - "
                          "part of the uniqueness key so different sources never overwrite each other")
    args = ap.parse_args(argv)

    s = Settings()
    if not s.has_supabase:
        sys.exit("SUPABASE_URL / SUPABASE_KEY (or SUPABASE_SERVICE_KEY) not set")
    client = make_supabase_client(s)

    instruments = [x.strip() for x in args.instruments.split(",")]
    timeframes = [x.strip() for x in args.timeframes.split(",")]

    mt4 = data.MT4Bars(s)
    try:
        for ins in instruments:
            need = data.bars_needed(args.years, "H1")
            print(f"{ins} H1: requesting {need} bars from MT4...")
            h1 = data.trim_history(data.drop_weekends(mt4.load(ins, "H1", need)), args.years)
            if h1.empty:
                print("  got 0 bars - check the symbol name and chart history in MT4")
                continue
            print(f"  got {len(h1)} bars, {(h1.index[-1] - h1.index[0]).days} days "
                  f"({h1.index[0]} -> {h1.index[-1]})")

            for tf in timeframes:
                if tf == "H1":
                    bars = h1
                else:
                    rule = RESAMPLE_RULE.get(tf)
                    if not rule:
                        print(f"  {tf}: no resample rule defined, skipping")
                        continue
                    bars = data.resample_ohlc(h1, rule)
                    print(f"  {tf}: derived {len(bars)} bars from H1 by resampling ({rule})")
                n = upsert_bars(client, s.prices_schema, s.prices_table, ins, tf, args.source, bars)
                print(f"  upserted {n} rows into {s.prices_schema}.{s.prices_table} ({tf})")
    finally:
        mt4.close()


if __name__ == "__main__":
    main()
