"""Pluggable historical price data sources sharing one canonical OHLCV schema.

Each source implements `DataSource.fetch()` and returns a DataFrame with the
same columns and a UTC-indexed timestamp, so bars from different providers
for the same instrument can be aligned and compared directly.
"""
from .base import CANONICAL_COLUMNS, DataSource, FetchRequest
from .histdata import HistDataSource

SOURCES = {
    "histdata": HistDataSource,
}

__all__ = ["CANONICAL_COLUMNS", "DataSource", "FetchRequest", "HistDataSource", "SOURCES"]
