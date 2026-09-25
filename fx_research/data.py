"""Loading H1 bars and slicing them into trading-day windows.

A "trading day" follows the FX convention: the day rolls over at 17:00 New York
time, so Sunday-evening bars belong to Monday and there are exactly five trading
days a week. Weekend bars (brokers sometimes print a few) are dropped.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

NY_TZ = "America/New_York"
OHLC = ["Open", "High", "Low", "Close", "Volume"]


def normalize_bars(df):
    """Return bars indexed by a UTC DatetimeIndex with Open/High/Low/Close/Volume columns."""
    df = df.copy()
    df.columns = [str(c).strip().title() for c in df.columns]
    if "Datetime" in df.columns:
        df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True)
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    if "Volume" not in df.columns:
        df["Volume"] = 0.0
    df = df[OHLC].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def trading_dates(index):
    """FX trading date of each timestamp (17:00 New York rollover)."""
    shifted = index.tz_convert(NY_TZ) + pd.Timedelta(hours=7)
    return pd.DatetimeIndex(shifted.date)


def drop_weekends(df):
    dates = trading_dates(df.index)
    return df[dates.dayofweek < 5]


def with_trading_date(df):
    df = drop_weekends(df)
    df = df.copy()
    df["TradeDate"] = trading_dates(df.index)
    return df


def last_trading_days(df, n_days):
    """Bars belonging to the last `n_days` trading days (weekends never counted)."""
    df = with_trading_date(df)
    days = df["TradeDate"].drop_duplicates()
    keep = days.iloc[-n_days:]
    return df[df["TradeDate"].isin(keep)].drop(columns="TradeDate")


def trim_history(df, years):
    start = df.index.max() - pd.Timedelta(days=int(round(365.25 * years)))
    return df[df.index >= start]


# ---------------------------------------------------------------------------
# Bar sources
# ---------------------------------------------------------------------------

def load_csv(instrument, csv_dir):
    """CSV like MT4-TradeSignals/currency_data/<PAIR>.csv (Datetime,Open,High,Low,Close,Volume,...)."""
    matches = glob.glob(os.path.join(csv_dir, f"{instrument}.csv")) or \
        glob.glob(os.path.join(csv_dir, f"{instrument}*.csv"))
    if not matches:
        raise FileNotFoundError(f"No CSV for {instrument} in {csv_dir}")
    return normalize_bars(pd.read_csv(matches[0]))


def load_supabase(client, instrument, settings, since):
    """Read H1 bars saved by MT4-TradeSignals (db.save_mkt_data) from public.fx_prices."""
    rows, page, start = [], 1000, 0
    table = client.schema(settings.prices_schema).table(settings.prices_table)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S%z")
    while True:
        resp = (table.select("Datetime,Open,High,Low,Close,Volume")
                .eq("Ccy", instrument)
                .gte("Datetime", since_str)
                .order("Datetime")
                .range(start, start + page - 1)
                .execute())
        rows.extend(resp.data or [])
        if not resp.data or len(resp.data) < page:
            break
        start += page
    if not rows:
        return pd.DataFrame(columns=OHLC)
    return normalize_bars(pd.DataFrame(rows))


class MT4Bars:
    """Pulls bars straight from the terminal through MT4-TradeSignals' ZMQ EA."""

    # MT4-TradeSignals.dataUtil treats EA timestamps as broker time (Asia/Dubai, UTC+3 for FTMO).
    broker_tz = os.environ.get("MT4_BROKER_TZ", "Asia/Dubai")

    def __init__(self, settings):
        if settings.mt4_tradesignals_path and settings.mt4_tradesignals_path not in sys.path:
            sys.path.insert(0, settings.mt4_tradesignals_path)
        from EACommunicator_API import EACommunicator_API  # noqa: E402 (lives in MT4-TradeSignals)
        self.api = EACommunicator_API()
        self.api.Connect(port=settings.mt4_port)

    def load(self, instrument, timeframe, n_bars):
        df = self.api.Get_last_x_bars_from_now(instrument=instrument, timeframe=timeframe, nbrofbars=n_bars)
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=OHLC)
        df.columns = ["Open", "High", "Low", "Close", "Volume", "Datetime"]
        df["Datetime"] = pd.to_datetime(df["Datetime"])
        if df["Datetime"].dt.tz is None:
            df["Datetime"] = df["Datetime"].dt.tz_localize(self.broker_tz)
        return normalize_bars(df)

    def close(self):
        self.api.Disconnect()


def bars_needed(years, timeframe="H1"):
    per_day = {"M15": 96, "M30": 48, "H1": 24, "H4": 6, "D1": 1}[timeframe]
    return int(np.ceil(years * 261 * per_day)) + 5 * per_day
