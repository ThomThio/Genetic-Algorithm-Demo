#!/usr/bin/env python3
"""One-off (or periodically re-run) mirror of Supabase price rows to local
CSV files, so run_discovery.py can run against a snapshot (--source local)
without hitting the network every time -- and keeps working if the Supabase
host is temporarily unreachable (e.g. a sandboxed environment whose network
policy blocks it, which is the situation this was written for).

Writes one CSV per (symbol, timeframe, source) combination to
data/prices/<symbol>_<timeframe>_<source>.csv, in the same standard shape
(timestamp index, lowercase open/high/low/close/volume columns) that
load_from_csv / load_from_local_cache expect -- so from run_discovery.py's
point of view, local and supabase sources are interchangeable.

Usage:
    export SUPABASE_URL="https://<project>.supabase.co"
    export SUPABASE_KEY="<anon-or-service-role-key>"

    # everything for one timeframe/source (auto-discovers symbols):
    python3 -m strategy.fetch_supabase_csv --timeframe H1 --source-name FTMO_MT4_demo

    # specific symbols only:
    python3 -m strategy.fetch_supabase_csv --symbols USDSGD,EURUSD --timeframe H1
"""
import argparse
import sys

from . import data as data_mod


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--table', default=data_mod.DEFAULT_TABLE)
    p.add_argument('--symbols', default=None, help='comma-separated; omit to auto-discover')
    p.add_argument('--symbol-col', default=data_mod.DEFAULT_SYMBOL_COL)
    p.add_argument('--timestamp-col', default=data_mod.DEFAULT_TIMESTAMP_COL)
    p.add_argument('--timeframe-col', default=data_mod.DEFAULT_TIMEFRAME_COL)
    p.add_argument('--timeframe', default='H1')
    p.add_argument('--source-col', default=data_mod.DEFAULT_SOURCE_COL)
    p.add_argument('--source-name', default=None,
                    help='filter the Source column (e.g. FTMO_MT4_demo); required if the table has more than one')
    p.add_argument('--cache-dir', default=data_mod.DEFAULT_LOCAL_DIR)
    args = p.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    else:
        print(f'Discovering symbols in {args.table} (timeframe={args.timeframe}, '
              f'source={args.source_name})...', file=sys.stderr)
        symbols = data_mod.list_supabase_symbols(
            args.table, args.symbol_col, timeframe=args.timeframe, timeframe_col=args.timeframe_col,
            source=args.source_name, source_col=args.source_col,
        )
        print(f'Found: {symbols}', file=sys.stderr)

    if not symbols:
        raise SystemExit('no symbols found/given -- pass --symbols explicitly')

    written = []
    for symbol in symbols:
        print(f'Fetching {symbol} ({args.timeframe}, {args.source_name})...', file=sys.stderr, end=' ')
        try:
            df = data_mod.load_from_supabase(
                table=args.table, symbol=symbol, symbol_col=args.symbol_col,
                timestamp_col=args.timestamp_col, timeframe=args.timeframe,
                timeframe_col=args.timeframe_col, source=args.source_name,
                source_col=args.source_col,
            )
        except ValueError as e:
            print(f'skipped ({e})', file=sys.stderr)
            continue
        path = data_mod.save_to_local_cache(df, symbol, args.timeframe, args.source_name, args.cache_dir)
        print(f'{len(df)} bars [{df.index[0]} .. {df.index[-1]}] -> {path}', file=sys.stderr)
        written.append(path)

    print(f'\nWrote {len(written)} CSV file(s) to {args.cache_dir}/', file=sys.stderr)
    print('Run discovery against this snapshot with:', file=sys.stderr)
    print(f'  python3 -m strategy.run_discovery --source local --cache-dir {args.cache_dir} '
          f'--timeframe {args.timeframe} --source-name {args.source_name}', file=sys.stderr)


if __name__ == '__main__':
    main()
