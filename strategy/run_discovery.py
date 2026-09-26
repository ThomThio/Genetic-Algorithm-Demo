#!/usr/bin/env python3
"""CLI: evolve long-only entry rules per symbol/slice and export the ones
with validated edge to JSON.

Examples:
    # demo run against synthetic data (no external dependencies beyond pip
    # install pandas numpy):
    python3 strategy/run_discovery.py --source synthetic

    # once Supabase is connected and SUPABASE_URL / SUPABASE_KEY are set:
    python3 strategy/run_discovery.py --source supabase --table ohlcv_daily \
        --symbols BTCUSD,ETHUSD,SPY

    # a local CSV per symbol (timestamp,open,high,low,close,volume columns):
    python3 strategy/run_discovery.py --source csv --csv-path data/SPY.csv --symbols SPY
"""
import argparse
import sys

from . import data as data_mod
from .backtest import run_backtest
from .fitness import MIN_TRADES_FOR_SIGNAL, OOS_MIN_TRADES, compute_edge
from .ga_engine import GENERATIONS, POP_SIZE, run_ga
from .genome import materialize_signal
from .indicators import compute_indicator_frame
from .interpretation import interpret
from .export import build_record, write_records
from .slicer import train_test_split_chrono


def _rule_preview(conditions) -> str:
    return ' AND '.join(c.name for c in conditions)


def _load_symbol(args, symbol: str):
    if args.source == 'synthetic':
        return data_mod.generate_synthetic(symbol, n_bars=args.n_bars, seed=args.seed)
    if args.source == 'csv':
        return data_mod.load_from_csv(args.csv_path)
    if args.source == 'supabase':
        return data_mod.load_from_supabase(
            table=args.table, symbol=symbol, symbol_col=args.symbol_col,
            timestamp_col=args.timestamp_col,
        )
    raise ValueError(f'unknown source: {args.source}')


def run_for_symbol(args, symbol: str):
    print(f'\n=== {symbol} ({args.source}) ===', file=sys.stderr)
    df = _load_symbol(args, symbol)
    if len(df) < 300:
        print(f'  skipping {symbol}: only {len(df)} bars, need >=300', file=sys.stderr)
        return []

    ind_full = compute_indicator_frame(df)
    train_df, test_df = train_test_split_chrono(df, args.train_frac)
    ind_train = ind_full.loc[train_df.index]
    ind_test = ind_full.loc[test_df.index]

    print(f'  train: {len(train_df)} bars [{train_df.index[0]} .. {train_df.index[-1]}]', file=sys.stderr)
    print(f'  test:  {len(test_df)} bars [{test_df.index[0]} .. {test_df.index[-1]}]', file=sys.stderr)

    def progress(gen, best):
        if gen % 5 == 0 or gen == args.generations - 1:
            print(f'  gen {gen:3d}  best_edge_score={best.fitness:+.3f}  '
                  f'n_trades={best.edge.n_trades}  rule={_rule_preview(best.spec.conditions)[:70]}',
                  file=sys.stderr)

    qualified = run_ga(train_df, ind_train, pop_size=args.pop_size,
                        generations=args.generations, seed=args.seed, progress_cb=progress)

    if not qualified:
        print(f'  no rule cleared the {MIN_TRADES_FOR_SIGNAL}-trade minimum on train data', file=sys.stderr)
        return []

    records = []
    for evaluated in qualified:
        oos_signal, oos_resolved = materialize_signal(evaluated.spec, test_df, ind_test)
        oos_trades = run_backtest(test_df, ind_test['atr_14'], oos_signal,
                                   evaluated.spec.stop_atr_mult, evaluated.spec.target_R,
                                   evaluated.spec.max_hold_bars)
        oos_edge = compute_edge(oos_trades)

        text = interpret(evaluated.resolved_conditions, evaluated.spec.stop_atr_mult,
                          evaluated.spec.target_R, evaluated.spec.max_hold_bars, evaluated.edge)
        text += (f' Out-of-sample check: {oos_edge.n_trades} trades, '
                 f'{oos_edge.win_rate:.0%} win rate, expectancy {oos_edge.expectancy_R:+.2f}R -- '
                 + ('holds up out of sample.' if oos_edge.edge_score > 0 and oos_edge.n_trades >= OOS_MIN_TRADES
                    else 'does NOT clearly hold out of sample; treat as unvalidated.'))

        record = build_record(
            symbol=symbol, data_source=args.source,
            train_range=(train_df.index[0], train_df.index[-1]),
            test_range=(test_df.index[0], test_df.index[-1]),
            evaluated=evaluated, oos_edge=oos_edge,
            resolved_conditions=evaluated.resolved_conditions, interpretation=text,
            generation_found=evaluated.gen_found,
            ga_params={'pop_size': args.pop_size, 'generations': args.generations,
                       'train_frac': args.train_frac},
        )
        records.append(record)

        flag = 'VALIDATED' if record['validated_out_of_sample'] else 'unvalidated'
        print(f'  [{flag}] edge={record["edge_score"]:+.3f}  '
              f'IS(n={evaluated.edge.n_trades},wr={evaluated.edge.win_rate:.0%},RR={evaluated.edge.reward_risk:.2f}) '
              f'OOS(n={oos_edge.n_trades},wr={oos_edge.win_rate:.0%},RR={oos_edge.reward_risk:.2f})  '
              f'{_rule_preview(evaluated.resolved_conditions)[:60]}', file=sys.stderr)

    # The GA often converges several nearby genomes onto the same condition
    # set with slightly different thresholds; keep only the best-scoring
    # variant per distinct condition set so the report shows genuinely
    # different algorithms rather than near-duplicates.
    best_per_condition_set = {}
    for r in records:
        key = frozenset(c['key'] for c in r['entry_rule']['conditions'])
        prev = best_per_condition_set.get(key)
        if prev is None or r['edge_score'] > prev['edge_score']:
            best_per_condition_set[key] = r

    deduped = sorted(best_per_condition_set.values(), key=lambda r: r['edge_score'], reverse=True)
    return deduped


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--source', choices=['synthetic', 'csv', 'supabase'], default='synthetic')
    p.add_argument('--symbols', default=None, help='comma-separated symbol list')
    p.add_argument('--csv-path', default=None)
    p.add_argument('--table', default='ohlcv_daily', help='Supabase table name')
    p.add_argument('--symbol-col', default='symbol')
    p.add_argument('--timestamp-col', default='timestamp')
    p.add_argument('--n-bars', type=int, default=1500, help='synthetic source only')
    p.add_argument('--train-frac', type=float, default=0.7)
    p.add_argument('--pop-size', type=int, default=POP_SIZE)
    p.add_argument('--generations', type=int, default=GENERATIONS)
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--out-dir', default='output/strategies')
    args = p.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    elif args.source == 'synthetic':
        symbols = data_mod.SYNTHETIC_SYMBOLS
    elif args.source == 'supabase':
        symbols = data_mod.list_supabase_symbols(args.table, args.symbol_col)
    else:
        raise SystemExit('--symbols is required for csv/supabase sources')

    all_records = []
    for symbol in symbols:
        all_records.extend(run_for_symbol(args, symbol))

    if not all_records:
        print('\nNo strategies cleared the minimum trade count on any slice.', file=sys.stderr)
        sys.exit(1)

    path = write_records(all_records, args.out_dir, run_name=args.source)

    validated = [r for r in all_records if r['validated_out_of_sample']]
    print(f'\nWrote {len(all_records)} strategies ({len(validated)} validated out-of-sample) -> {path}',
          file=sys.stderr)
    print('\nTop validated strategies by out-of-sample edge:', file=sys.stderr)
    for r in sorted(validated, key=lambda r: r['edge_score'], reverse=True)[:10]:
        oos = r['performance']['out_of_sample']
        print(f"  {r['universe_slice']['symbol']:22s} edge={r['edge_score']:+.3f} "
              f"wr={oos['win_rate']:.0%} RR={oos['reward_risk']:.2f} n={oos['n_trades']:3d}  "
              f"{r['entry_rule']['rule_text'][:70]}", file=sys.stderr)


if __name__ == '__main__':
    main()
