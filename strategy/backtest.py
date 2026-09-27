"""Long-only trade simulator for a single boolean entry-signal series.

Entries are taken at the NEXT bar's open after a signal bar (no lookahead).
Exit is whichever of a fixed ATR-based stop or R-multiple target is hit
first, walking bar-by-bar up to a max holding period, else a time exit at
the close of the last allowed bar. If a single bar's range contains both
the stop and the target, the stop is assumed to have been hit first (the
conservative assumption). Trades do not overlap: after an exit, scanning
resumes at the following bar.
"""
from dataclasses import dataclass, asdict
from typing import List

import numpy as np
import pandas as pd


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    r_multiple: float
    bars_held: int
    exit_reason: str

    def to_dict(self):
        return asdict(self)


def run_backtest(df: pd.DataFrame, atr_series: pd.Series, signal: pd.Series,
                  stop_atr_mult: float, target_R: float, max_hold_bars: int) -> List[Trade]:
    n = len(df)
    opens = df['open'].to_numpy()
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    atr_vals = atr_series.to_numpy()
    sig = signal.to_numpy()
    idx = df.index

    trades: List[Trade] = []
    i = 0
    while i < n - 1:
        a = atr_vals[i]
        if sig[i] and not np.isnan(a) and a > 0:
            entry_idx = i + 1
            entry_price = opens[entry_idx]
            risk = stop_atr_mult * a
            stop_price = entry_price - risk
            target_price = entry_price + risk * target_R

            exit_price = None
            exit_idx = None
            exit_reason = None
            last_bar = min(entry_idx + max_hold_bars - 1, n - 1)
            for j in range(entry_idx, last_bar + 1):
                hit_stop = lows[j] <= stop_price
                hit_target = highs[j] >= target_price
                if hit_stop:
                    exit_price, exit_idx, exit_reason = stop_price, j, 'stop'
                    break
                if hit_target:
                    exit_price, exit_idx, exit_reason = target_price, j, 'target'
                    break

            if exit_price is None:
                exit_idx = last_bar
                exit_price = closes[last_bar]
                exit_reason = 'time'

            r_multiple = (exit_price - entry_price) / risk if risk > 0 else 0.0
            trades.append(Trade(
                entry_time=str(idx[entry_idx]),
                exit_time=str(idx[exit_idx]),
                entry_price=round(float(entry_price), 6),
                exit_price=round(float(exit_price), 6),
                r_multiple=round(float(r_multiple), 4),
                bars_held=int(exit_idx - entry_idx + 1),
                exit_reason=exit_reason,
            ))
            i = exit_idx + 1
        else:
            i += 1

    return trades
