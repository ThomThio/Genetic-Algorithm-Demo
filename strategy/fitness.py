"""Edge computation: turns a list of trades into the stats that define
strategy success, per the brief -- "success is defined by Edge".

Edge here means realized expectancy in R-multiples (average R per trade),
but the GA is scored on a confidence-adjusted, sample-size-penalized
version of it so it doesn't reward rules that got lucky on a handful of
trades. That's `edge_score`.

This is also the one place "backtest basics" (net profit, profit factor,
Sharpe, P&L:drawdown, trade count) get computed, since every context that
needs them -- GA search, in-sample, out-of-sample, robustness-test variants,
live/paper tracking -- already funnels a list of Trade objects through
compute_edge(). Computing them here once means every comparison across
environments, instruments, and strategies is on identically-defined numbers.
"""
import math
from dataclasses import dataclass, asdict
from typing import List, Optional

import numpy as np
import pandas as pd

from .backtest import Trade

MIN_TRADES_FOR_SIGNAL = 30  # in-sample / significance floor (per house standard)
OOS_MIN_TRADES = 10         # out-of-sample floor: smaller because the test fold is smaller


@dataclass
class EdgeStats:
    n_trades: int
    win_rate: float
    win_rate_lb95: float  # Wilson lower bound, 95%
    avg_win_R: float
    avg_loss_R: float
    reward_risk: float
    profit_factor: float
    expectancy_R: float       # mean R per trade = the raw "edge"
    edge_score: float         # confidence-adjusted edge used to rank/select
    net_profit_R: float       # sum of R multiples = total P&L in risk-normalized units
    max_drawdown_R: float     # worst peak-to-trough on the cumulative-R equity curve
    pnl_to_dd_ratio: float    # net_profit_R / max_drawdown_R
    sharpe_ratio: Optional[float]  # annualized, using trade frequency (None if undatable/undefined)
    meets_min_trade_count: bool    # n_trades >= MIN_TRADES_FOR_SIGNAL (statistical significance floor)

    def to_dict(self):
        return asdict(self)


def _wilson_lower_bound(wins: int, n: int, z: float = 1.959963985) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _equity_curve_R(r_values: List[float]) -> np.ndarray:
    return np.cumsum(r_values)


def _max_drawdown_R(cum_curve: np.ndarray) -> float:
    if len(cum_curve) == 0:
        return 0.0
    running_peak = np.maximum.accumulate(cum_curve)
    drawdowns = running_peak - cum_curve
    return float(drawdowns.max())


def _trade_span_years(trades: List[Trade]) -> Optional[float]:
    times = []
    for t in trades:
        for raw in (t.entry_time, t.exit_time):
            if not raw:
                continue
            try:
                times.append(pd.Timestamp(raw))
            except (ValueError, TypeError):
                continue
    if len(times) < 2:
        return None
    span_days = (max(times) - min(times)).total_seconds() / 86400.0
    return span_days / 365.25 if span_days > 0 else None


def _annualized_sharpe(trades: List[Trade], r_values: List[float]) -> Optional[float]:
    n = len(r_values)
    if n < 2:
        return None
    std_r = float(np.std(r_values, ddof=1))
    if std_r == 0:
        return None
    span_years = _trade_span_years(trades)
    if not span_years or span_years <= 0:
        return None  # can't annualize without usable entry/exit timestamps
    trades_per_year = n / span_years
    mean_r = float(np.mean(r_values))
    return (mean_r / std_r) * math.sqrt(trades_per_year)


def compute_edge(trades: List[Trade]) -> EdgeStats:
    n = len(trades)
    if n == 0:
        return EdgeStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -10.0,
                          net_profit_R=0.0, max_drawdown_R=0.0, pnl_to_dd_ratio=0.0,
                          sharpe_ratio=None, meets_min_trade_count=False)

    r_values = [t.r_multiple for t in trades]
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r <= 0]

    win_rate = len(wins) / n
    win_rate_lb = _wilson_lower_bound(len(wins), n)
    avg_win_R = sum(wins) / len(wins) if wins else 0.0
    avg_loss_R = abs(sum(losses) / len(losses)) if losses else 0.0
    reward_risk = (avg_win_R / avg_loss_R) if avg_loss_R > 0 else float(avg_win_R) if avg_win_R > 0 else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float('inf') if gross_win > 0 else 0.0)
    expectancy_R = sum(r_values) / n

    # Confidence-adjusted edge: use the Wilson lower-bound win rate (instead
    # of the raw win rate) against the *realized* win/loss R sizes. This
    # shrinks the score for small samples toward a pessimistic estimate
    # without needing a hard trade-count cutoff baked into the number
    # itself -- callers apply their own minimum-sample gate (see
    # MIN_TRADES_FOR_SIGNAL / OOS_MIN_TRADES) on top of this true estimate.
    edge_score = win_rate_lb * avg_win_R - (1 - win_rate_lb) * avg_loss_R

    pf_capped = profit_factor if profit_factor != float('inf') else 999.0

    cum_curve = _equity_curve_R(r_values)
    net_profit_R = float(cum_curve[-1])
    max_dd = _max_drawdown_R(cum_curve)
    if max_dd > 0:
        pnl_to_dd = net_profit_R / max_dd
    else:
        pnl_to_dd = 999.0 if net_profit_R > 0 else 0.0
    sharpe = _annualized_sharpe(trades, r_values)

    return EdgeStats(
        n_trades=n,
        win_rate=round(win_rate, 4),
        win_rate_lb95=round(win_rate_lb, 4),
        avg_win_R=round(avg_win_R, 4),
        avg_loss_R=round(avg_loss_R, 4),
        reward_risk=round(reward_risk, 4),
        profit_factor=round(pf_capped, 4),
        expectancy_R=round(expectancy_R, 4),
        edge_score=round(edge_score, 4),
        net_profit_R=round(net_profit_R, 4),
        max_drawdown_R=round(max_dd, 4),
        pnl_to_dd_ratio=round(pnl_to_dd, 4),
        sharpe_ratio=round(sharpe, 4) if sharpe is not None else None,
        meets_min_trade_count=n >= MIN_TRADES_FOR_SIGNAL,
    )
