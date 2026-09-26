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
    rows = [
        ('Trades', is_['n_trades'], oos['n_trades']),
        ('Win rate', f"{is_['win_rate']:.0%}", f"{oos['win_rate']:.0%}"),
        ('Win rate (Wilson LB 95%)', f"{is_['win_rate_lb95']:.0%}", f"{oos['win_rate_lb95']:.0%}"),
        ('Avg win (R)', is_['avg_win_R'], oos['avg_win_R']),
        ('Avg loss (R)', is_['avg_loss_R'], oos['avg_loss_R']),
        ('Reward:Risk', is_['reward_risk'], oos['reward_risk']),
        ('Profit factor', is_['profit_factor'], oos['profit_factor']),
        ('Expectancy (R/trade)', is_['expectancy_R'], oos['expectancy_R']),
        ('Edge score', is_['edge_score'], oos['edge_score']),
    ]
    lines = ['| Metric | In-Sample (train) | Out-of-Sample (test) |', '|---|---|---|']
    for name, a, b in rows:
        lines.append(f'| {name} | {a} | {b} |')
    return '\n'.join(lines)


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
    index_rows = ['| Symbol | Validated | Edge | Win rate (OOS) | R:R (OOS) | Trades (OOS) | Rule | Report |',
                   '|---|---|---|---|---|---|---|---|']

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
        index_rows.append(
            f"| {r['universe_slice']['symbol']} | {'YES' if r['validated_out_of_sample'] else 'no'} | "
            f"{r['edge_score']:+.3f} | {oos['win_rate']:.0%} | {oos['reward_risk']:.2f} | {oos['n_trades']} | "
            f"{r['entry_rule']['rule_text']} | [{sid[:8]}]({sid}.md) |"
        )

    index_path = os.path.join(reports_dir, f'summary_{run_name}.md')
    with open(index_path, 'w') as f:
        f.write(f"# Discovery run summary: {run_name}\n\n" + '\n'.join(index_rows) + '\n')
    paths.append(index_path)

    return paths
