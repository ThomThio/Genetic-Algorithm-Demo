"""Edge computation: turns a list of trades into the stats that define
strategy success, per the brief -- "success is defined by Edge".

Edge here means realized expectancy in R-multiples (average R per trade),
but the GA is scored on a confidence-adjusted, sample-size-penalized
version of it so it doesn't reward rules that got lucky on a handful of
trades. That's `edge_score`.
"""
import math
from dataclasses import dataclass, asdict
from typing import List

from .backtest import Trade

MIN_TRADES_FOR_SIGNAL = 20  # in-sample floor: GA search discards rules below this
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
    expectancy_R: float   # mean R per trade = the raw "edge"
    edge_score: float     # confidence-adjusted edge used to rank/select

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


def compute_edge(trades: List[Trade]) -> EdgeStats:
    n = len(trades)
    if n == 0:
        return EdgeStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -10.0)

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
    )
