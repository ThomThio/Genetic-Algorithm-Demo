"""Turns discovered-strategy records into two things per strategy:

1. A human-readable Markdown scorecard (output/reports/<id>.md) with the
   entry rule, risk model, interpretation, and backtest performance table.
2. A live-tracking JSON seed (output/live_tracking/<id>.json) holding the
   backtest baseline plus an empty `live_trades` log, meant to be appended
   to over time (see track_live.py) so realized live/paper performance can
   be compared against the backtest numbers on the same Edge metric.

Also writes one combined summary index per discovery run so results across
strategies/symbols can be scanned at a glance.
"""
import json
import os
from typing import List


def _perf_table(perf: dict) -> str:
    is_, oos = perf['in_sample'], perf['out_of_sample']
    sharpe_is = f"{is_['sharpe_ratio']:.2f}" if is_['sharpe_ratio'] is not None else 'n/a'
    sharpe_oos = f"{oos['sharpe_ratio']:.2f}" if oos['sharpe_ratio'] is not None else 'n/a'
    rows = [
        ('Trades', is_['n_trades'], oos['n_trades']),
        ('Meets min trade count (30)', is_['meets_min_trade_count'], oos['meets_min_trade_count']),
        ('Win rate', f"{is_['win_rate']:.0%}", f"{oos['win_rate']:.0%}"),
        ('Win rate (Wilson LB 95%)', f"{is_['win_rate_lb95']:.0%}", f"{oos['win_rate_lb95']:.0%}"),
        ('Avg win (R)', is_['avg_win_R'], oos['avg_win_R']),
        ('Avg loss (R)', is_['avg_loss_R'], oos['avg_loss_R']),
        ('Reward:Risk', is_['reward_risk'], oos['reward_risk']),
        ('Profit factor', is_['profit_factor'], oos['profit_factor']),
        ('Net profit (R)', is_['net_profit_R'], oos['net_profit_R']),
        ('Max drawdown (R)', is_['max_drawdown_R'], oos['max_drawdown_R']),
        ('P&L : Drawdown ratio', is_['pnl_to_dd_ratio'], oos['pnl_to_dd_ratio']),
        ('Sharpe ratio (annualized)', sharpe_is, sharpe_oos),
        ('Expectancy (R/trade)', is_['expectancy_R'], oos['expectancy_R']),
        ('Edge score', is_['edge_score'], oos['edge_score']),
    ]
    lines = ['| Metric | In-Sample (train) | Out-of-Sample (test) |', '|---|---|---|']
    for name, a, b in rows:
        lines.append(f'| {name} | {a} | {b} |')
    return '\n'.join(lines)


def _robustness_section(robustness: dict) -> str:
    vr = robustness.get('vs_random_distribution', {})
    nt = robustness.get('noise_test', {})
    pt = robustness.get('permutation_test', {})

    def _fmt(d: dict, rows: list) -> str:
        if 'note' in d and len(d) <= 3:
            return f"_{d['note']}_"
        lines = ['| Metric | Value |', '|---|---|']
        for label, key in rows:
            lines.append(f'| {label} | {d.get(key, "n/a")} |')
        return '\n'.join(lines)

    vr_table = _fmt(vr, [
        ('Random draws', 'n_random'), ('Signals matched', 'n_signals_matched'),
        ('Real edge score', 'real_edge_score'), ('Random median edge score', 'random_median_edge_score'),
        ('Random P90 edge score', 'random_p90_edge_score'),
        ('Percentile rank vs. random', 'percentile_rank_vs_random'),
        ('Beats random at 95th pct', 'beats_random_at_95pct'),
    ])
    nt_table = _fmt(nt, [
        ('Variants', 'n_variants'), ('Noise %', 'noise_pct'),
        ('Median edge score', 'median_edge_score'), ('P10 edge score', 'p10_edge_score'),
        ('P90 edge score', 'p90_edge_score'), ('Spread ratio (P90-P10)/median', 'spread_ratio'),
        ('Robust (spread < 0.5)', 'robust_spread_lt_0_5'),
    ])
    pt_table = _fmt(pt, [
        ('Shuffles', 'n_shuffles'), ('Real edge score', 'real_edge_score'),
        ('Shuffled median edge score', 'shuffled_median_edge_score'),
        ('Shuffled P90 edge score', 'shuffled_p90_edge_score'),
        ('Percentile rank vs. shuffled', 'percentile_rank_vs_shuffled'),
        ('Depends on real sequence at 95th pct', 'depends_on_real_sequence_at_95pct'),
    ])

    return f"""## Robustness & overfitting checks

Run only because this strategy already passed the in-sample/out-of-sample
walk-forward gate above; these ask three further questions of the *same*
rule and risk model.

### vs. random distribution

Is the edge better than randomly-timed entries with the same trade count and risk model?

{vr_table}

### Noise test

Does the edge survive small perturbations of the OHLC data (same rule, same data, jittered prices)?

{nt_table}

### Permutation test

Does the edge depend on genuine sequential/temporal structure, or just the return distribution?

{pt_table}
"""


def build_markdown_report(record: dict) -> str:
    sid = record['strategy_id']
    slice_ = record['universe_slice']
    rule = record['entry_rule']
    risk = record['risk_model']
    validated = record['validated_out_of_sample']

    cond_lines = '\n'.join(
        f"- {c['name']}" + (f" ({', '.join(f'{k}={v}' for k, v in c['params'].items())})" if c['params'] else '')
        for c in rule['conditions']
    )

    return f"""# Strategy Report: {slice_['symbol']} — {rule['rule_text']}

**Strategy ID:** `{sid}`
**Direction:** Long only
**Data source:** {record['data_source']}
**Created:** {record['created_at']}
**Train range:** {slice_['train_range'][0]} .. {slice_['train_range'][1]}
**Test range:** {slice_['test_range'][0]} .. {slice_['test_range'][1]}
**GA meta:** generation {record['ga_meta']['generation_found']}, pop={record['ga_meta']['pop_size']}, gens={record['ga_meta']['generations']}
**Genome signature:** `{record['ga_meta']['genome_signature']}`

## Entry rule ({rule['logic']} of all conditions below)

{cond_lines}

## Risk model

- Stop: {risk['stop_atr_mult']}x ATR from entry
- Target: {risk['target_R']}R
- Max hold: {risk['max_hold_bars']} bars
- Entry fill: {risk['entry_fill']}

## Why this signal

{record['interpretation']}

## Backtest performance

{_perf_table(record['performance'])}

**Validated out-of-sample:** {'YES' if validated else 'no — treat as unvalidated / exploratory'}
**Headline edge score:** {record['edge_score']:+.4f}

{_robustness_section(record['robustness']) if record.get('robustness') else ''}
## Live tracking log

Log each live or paper trade against this exact rule with:

    python -m strategy.track_live --strategy-id {sid} --r-multiple <R> \\
        [--entry-time ...] [--exit-time ...] [--note "..."]

| # | Entry time | Exit time | R multiple | Win/Loss | Note |
|---|---|---|---|---|---|
| _(no live trades logged yet)_ | | | | | |

### Live vs backtest comparison

_(filled in automatically by `track_live.py` once trades are logged — compares
realized win rate / R:R / expectancy against the out-of-sample backtest
numbers above, on the same Edge metric.)_
"""


def write_reports(records: List[dict], reports_dir: str, tracking_dir: str, run_name: str) -> List[str]:
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(tracking_dir, exist_ok=True)

    paths = []
    index_rows = ['| Symbol | Validated | Robustness | Edge | Win rate (OOS) | R:R (OOS) | Sharpe (OOS) | '
                  'Trades (OOS) | Rule | Report |',
                  '|---|---|---|---|---|---|---|---|---|---|']

    for r in records:
        sid = r['strategy_id']
        md_path = os.path.join(reports_dir, f'{sid}.md')
        with open(md_path, 'w') as f:
            f.write(build_markdown_report(r))
        paths.append(md_path)

        tracking_path = os.path.join(tracking_dir, f'{sid}.json')
        if not os.path.exists(tracking_path):
            with open(tracking_path, 'w') as f:
                json.dump({
                    'strategy_id': sid,
                    'symbol': r['universe_slice']['symbol'],
                    'rule_text': r['entry_rule']['rule_text'],
                    'risk_model': r['risk_model'],
                    'backtest_baseline': r['performance']['out_of_sample'],
                    'live_trades': [],
                }, f, indent=2)

        oos = r['performance']['out_of_sample']
        sharpe_str = f"{oos['sharpe_ratio']:.2f}" if oos['sharpe_ratio'] is not None else 'n/a'
        robustness = r.get('robustness')
        if not robustness:
            rob_str = '—'
        else:
            checks = [
                robustness.get('vs_random_distribution', {}).get('beats_random_at_95pct'),
                robustness.get('noise_test', {}).get('robust_spread_lt_0_5'),
                robustness.get('permutation_test', {}).get('depends_on_real_sequence_at_95pct'),
            ]
            passed = sum(1 for c in checks if c is True)
            evaluable = sum(1 for c in checks if c is not None)
            rob_str = f'{passed}/{evaluable} passed' if evaluable else 'inconclusive'
        index_rows.append(
            f"| {r['universe_slice']['symbol']} | {'YES' if r['validated_out_of_sample'] else 'no'} | {rob_str} | "
            f"{r['edge_score']:+.3f} | {oos['win_rate']:.0%} | {oos['reward_risk']:.2f} | {sharpe_str} | "
            f"{oos['n_trades']} | {r['entry_rule']['rule_text']} | [{sid[:8]}]({sid}.md) |"
        )

    index_path = os.path.join(reports_dir, f'summary_{run_name}.md')
    with open(index_path, 'w') as f:
        f.write(f"# Discovery run summary: {run_name}\n\n" + '\n'.join(index_rows) + '\n')
    paths.append(index_path)

    return paths
