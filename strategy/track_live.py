#!/usr/bin/env python3
"""Log a live/paper trade against a previously discovered strategy and
compare realized performance to its backtest out-of-sample baseline, using
the exact same Edge metric (Wilson-lower-bound-adjusted expectancy).

The strategy's scorecard (output/reports/<id>.md) is the fixed spec; this
writes a separate, append-only ledger (output/reports/<id>_live.md) so
logging results never touches the original report.

Usage:
    python -m strategy.track_live --strategy-id <id> --r-multiple 1.8 \
        --entry-time 2026-09-20T14:30:00 --exit-time 2026-09-22T10:00:00 \
        --note "clean trend day"

    # just check current live-vs-backtest status without logging a trade:
    python -m strategy.track_live --strategy-id <id> --status-only
"""
import argparse
import json
import os
from datetime import datetime, timezone

from .backtest import Trade
from .fitness import compute_edge


def _load(tracking_path: str) -> dict:
    if not os.path.exists(tracking_path):
        raise SystemExit(
            f'no tracking file at {tracking_path}. It is created automatically the first time '
            'run_discovery.py exports a strategy -- check the strategy id and --tracking-dir.'
        )
    with open(tracking_path) as f:
        return json.load(f)


def _save(tracking_path: str, data: dict):
    with open(tracking_path, 'w') as f:
        json.dump(data, f, indent=2)


def _live_trades_as_trade_objs(live_trades):
    return [
        Trade(
            entry_time=t.get('entry_time') or '',
            exit_time=t.get('exit_time') or '',
            entry_price=t.get('entry_price') or 0.0,
            exit_price=t.get('exit_price') or 0.0,
            r_multiple=t['r_multiple'],
            bars_held=t.get('bars_held') or 0,
            exit_reason=t.get('exit_reason') or 'live',
        )
        for t in live_trades
    ]


def _comparison_table(live_stats, baseline: dict) -> str:
    live_sharpe = f'{live_stats.sharpe_ratio:.2f}' if live_stats.sharpe_ratio is not None else 'n/a'
    bt_sharpe = f"{baseline['sharpe_ratio']:.2f}" if baseline['sharpe_ratio'] is not None else 'n/a'
    rows = [
        ('Trades', live_stats.n_trades, baseline['n_trades']),
        ('Meets min trade count (30)', live_stats.meets_min_trade_count, baseline['meets_min_trade_count']),
        ('Win rate', f'{live_stats.win_rate:.0%}', f"{baseline['win_rate']:.0%}"),
        ('Win rate (Wilson LB 95%)', f'{live_stats.win_rate_lb95:.0%}', f"{baseline['win_rate_lb95']:.0%}"),
        ('Reward:Risk', live_stats.reward_risk, baseline['reward_risk']),
        ('Profit factor', live_stats.profit_factor, baseline['profit_factor']),
        ('Net profit (R)', live_stats.net_profit_R, baseline['net_profit_R']),
        ('Max drawdown (R)', live_stats.max_drawdown_R, baseline['max_drawdown_R']),
        ('P&L : Drawdown ratio', live_stats.pnl_to_dd_ratio, baseline['pnl_to_dd_ratio']),
        ('Sharpe ratio (annualized)', live_sharpe, bt_sharpe),
        ('Expectancy (R/trade)', live_stats.expectancy_R, baseline['expectancy_R']),
        ('Edge score', live_stats.edge_score, baseline['edge_score']),
    ]
    lines = ['| Metric | Live so far | Backtest (OOS) baseline |', '|---|---|---|']
    for name, live_v, bt_v in rows:
        lines.append(f'| {name} | {live_v} | {bt_v} |')
    return '\n'.join(lines)


def render_live_markdown(data: dict, live_stats) -> str:
    trades = data['live_trades']
    table_rows = ['| # | Entry time | Exit time | R multiple | Win/Loss | Note |', '|---|---|---|---|---|---|']
    for i, t in enumerate(trades, 1):
        wl = 'Win' if t['r_multiple'] > 0 else 'Loss'
        table_rows.append(
            f"| {i} | {t.get('entry_time') or ''} | {t.get('exit_time') or ''} | "
            f"{t['r_multiple']:+.2f} | {wl} | {t.get('note') or ''} |"
        )
    trades_table = '\n'.join(table_rows) if trades else '_(no live trades logged yet)_'

    verdict = 'insufficient live sample yet to compare' if live_stats.n_trades < 5 else (
        'live edge is holding up vs. backtest' if live_stats.edge_score >= 0.5 * data['backtest_baseline']['edge_score']
        else 'live edge is materially underperforming the backtest -- worth re-checking the rule / regime')

    return f"""# Live tracking ledger: {data['symbol']} — {data['rule_text']}

**Strategy ID:** `{data['strategy_id']}`
**Last updated:** {datetime.now(timezone.utc).isoformat()}

## Logged trades

{trades_table}

## Live vs backtest comparison

{_comparison_table(live_stats, data['backtest_baseline'])}

**Read:** {verdict}. Live sample sizes will be small for a while -- treat
anything under ~20 live trades as too noisy to draw firm conclusions from,
same as the backtest's own minimum-sample rule.
"""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--strategy-id', required=True)
    p.add_argument('--r-multiple', type=float, default=None, help='realized R multiple for this trade')
    p.add_argument('--entry-time', default=None)
    p.add_argument('--exit-time', default=None)
    p.add_argument('--entry-price', type=float, default=None)
    p.add_argument('--exit-price', type=float, default=None)
    p.add_argument('--bars-held', type=int, default=None)
    p.add_argument('--note', default='')
    p.add_argument('--status-only', action='store_true', help="don't log a trade, just print current status")
    p.add_argument('--tracking-dir', default='output/live_tracking')
    p.add_argument('--reports-dir', default='output/reports')
    args = p.parse_args()

    tracking_path = os.path.join(args.tracking_dir, f'{args.strategy_id}.json')
    data = _load(tracking_path)

    if not args.status_only:
        if args.r_multiple is None:
            raise SystemExit('--r-multiple is required unless --status-only is set')
        data['live_trades'].append({
            'entry_time': args.entry_time,
            'exit_time': args.exit_time,
            'entry_price': args.entry_price,
            'exit_price': args.exit_price,
            'r_multiple': args.r_multiple,
            'bars_held': args.bars_held,
            'note': args.note,
            'logged_at': datetime.now(timezone.utc).isoformat(),
        })
        _save(tracking_path, data)

    live_trades = _live_trades_as_trade_objs(data['live_trades'])
    live_stats = compute_edge(live_trades)

    os.makedirs(args.reports_dir, exist_ok=True)
    live_md_path = os.path.join(args.reports_dir, f'{args.strategy_id}_live.md')
    with open(live_md_path, 'w') as f:
        f.write(render_live_markdown(data, live_stats))

    print(f"{'Logged 1 trade. ' if not args.status_only else ''}"
          f"{live_stats.n_trades} live trades so far for {data['symbol']} / {data['rule_text']}")
    print(_comparison_table(live_stats, data['backtest_baseline']))
    print(f'\nFull ledger written to {live_md_path}')


if __name__ == '__main__':
    main()
