"""'Slice and dice' helpers: chronological train/test split for walk-forward
validation, the main defense here against reporting a curve-fit rule as
having edge (a rule only counts once it clears MIN_TRADES_FOR_SIGNAL and
keeps a positive edge_score on data the GA never optimized against).
"""
from typing import Tuple

import pandas as pd


def train_test_split_chrono(df: pd.DataFrame, train_frac: float = 0.7) -> Tuple[pd.DataFrame, pd.DataFrame]:
    cut = int(len(df) * train_frac)
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()
