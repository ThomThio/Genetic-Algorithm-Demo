"""Common interface and canonical schema for historical price data sources."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

import pandas as pd

CANONICAL_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class FetchRequest:
    instrument: str  # e.g. "EURUSD"
    timeframe: str  # e.g. "H1"
    start: date
    end: date


class DataSource(ABC):
    """A provider of historical OHLCV bars for an instrument."""

    name: str

    @abstractmethod
    def fetch(self, request: FetchRequest) -> pd.DataFrame:
        """Return a DataFrame indexed by UTC timestamp with CANONICAL_COLUMNS."""
        raise NotImplementedError

    @staticmethod
    def normalize(df: pd.DataFrame) -> pd.DataFrame:
        """Enforce canonical dtypes/index, drop duplicate bars, sort by time."""
        df = df[CANONICAL_COLUMNS].astype(float)
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "timestamp"
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
