"""Data loading: real Supabase OHLCV data (once connected) or a synthetic
demo dataset used to exercise the whole pipeline end-to-end in the meantime.

Supabase usage (once the connector/credentials are available):

    export SUPABASE_URL="https://<project>.supabase.co"
    export SUPABASE_KEY="<service-role-or-anon-key>"
    python3 strategy/run_discovery.py --source supabase --table ohlcv_daily \
        --symbols BTCUSD,ETHUSD

The loader expects (and you can remap via --col-* flags) a table with one
row per bar: a symbol column, a timestamp column, and open/high/low/close/
volume columns. Anything else (e.g. a `bid`/`ask` schema, multiple tables
per symbol) needs a small adapter here once the real schema is known.
"""
import os
from typing import List, Optional

import numpy as np
import pandas as pd

REQUIRED_COLS = ['open', 'high', 'low', 'close', 'volume']


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_index()
    df = df[~df.index.duplicated(keep='first')]
    for c in REQUIRED_COLS:
        if c not in df.columns:
            raise ValueError(f'missing required column: {c}')
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.dropna(subset=REQUIRED_COLS)


def load_from_csv(path: str, timestamp_col: str = 'timestamp') -> pd.DataFrame:
    df = pd.read_csv(path)
    df[timestamp_col] = pd.to_datetime(df[timestamp_col])
    df = df.set_index(timestamp_col)
    return _finalize(df)


def load_from_supabase(table: str, symbol: Optional[str] = None,
                        symbol_col: str = 'symbol', timestamp_col: str = 'timestamp',
                        col_map: Optional[dict] = None, page_size: int = 5000) -> pd.DataFrame:
    """Pulls one symbol's OHLCV rows from a Supabase table via the
    supabase-py client, paginating with .range() since PostgREST caps
    single-request row counts.
    """
    from supabase import create_client  # imported lazily: optional dependency

    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) must be set '
            'in the environment to load from Supabase.'
        )
    client = create_client(url, key)

    rows = []
    start = 0
    while True:
        q = client.table(table).select('*')
        if symbol is not None:
            q = q.eq(symbol_col, symbol)
        q = q.order(timestamp_col).range(start, start + page_size - 1)
        resp = q.execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    if not rows:
        raise ValueError(f'no rows returned for table={table!r} symbol={symbol!r}')

    df = pd.DataFrame(rows)
    if col_map:
        df = df.rename(columns=col_map)
    df[timestamp_col] = pd.to_datetime(df[timestamp_col])
    df = df.set_index(timestamp_col)
    return _finalize(df)


def list_supabase_symbols(table: str, symbol_col: str = 'symbol') -> List[str]:
    from supabase import create_client

    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError('SUPABASE_URL and SUPABASE_KEY must be set in the environment.')
    client = create_client(url, key)
    resp = client.table(table).select(symbol_col).execute()
    return sorted({row[symbol_col] for row in (resp.data or [])})


# --- Synthetic demo data -------------------------------------------------
# NOT real market data. Used only so the GA + backtest + edge pipeline can
# be demonstrated end-to-end before real Supabase credentials are wired up.
# Each series has a deliberately different, injected regime so the GA has
# genuinely different structure to find per "slice" -- a sanity check that
# the search mechanism recovers the archetype it should, not a claim about
# real market edge.

SYNTHETIC_SYMBOLS = ['DEMO_TREND_PULLBACK', 'DEMO_MEAN_REVERT', 'DEMO_VOL_BREAKOUT']


def _ohlcv_from_close(close: np.ndarray, base_vol: float, vol_spikes: np.ndarray,
                       start: str, freq: str, rng: np.random.Generator) -> pd.DataFrame:
    n = len(close)
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1] * (1 + rng.normal(0, 0.0008, n - 1))
    intrabar = np.abs(rng.normal(0, 0.004, n)) * close
    high = np.maximum(open_, close) + intrabar
    low = np.minimum(open_, close) - intrabar
    volume = base_vol * np.exp(rng.normal(0, 0.25, n)) * (1 + vol_spikes)

    idx = pd.date_range(start=start, periods=n, freq=freq)
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close,
                          'volume': volume}, index=idx)


def _gen_trend_pullback(n: int, rng: np.random.Generator) -> pd.DataFrame:
    returns = np.zeros(n)
    drift = 0.00035
    mom = 0.0
    for t in range(n):
        mom = 0.85 * mom + 0.15 * returns[t - 1] if t > 0 else 0.0
        shock = rng.normal(0, 0.009)
        # occasional multi-bar pullback regime
        pullback = -0.0025 if (t % 47) < 6 else 0.0
        returns[t] = drift + 0.35 * mom + pullback + shock
    close = 100 * np.cumprod(1 + returns)
    spikes = (np.abs(returns) > np.percentile(np.abs(returns), 90)).astype(float)
    return _ohlcv_from_close(close, base_vol=1_000_000, vol_spikes=spikes,
                              start='2019-01-01', freq='D', rng=rng)


def _gen_mean_revert(n: int, rng: np.random.Generator) -> pd.DataFrame:
    level = 100.0
    price = np.zeros(n)
    theta, mu, sigma = 0.06, 100.0, 1.1
    for t in range(n):
        level = level + theta * (mu - level) + rng.normal(0, sigma)
        price[t] = level
    spikes = np.zeros(n)
    return _ohlcv_from_close(price, base_vol=800_000, vol_spikes=spikes,
                              start='2019-01-01', freq='D', rng=rng)


def _gen_vol_breakout(n: int, rng: np.random.Generator) -> pd.DataFrame:
    returns = np.zeros(n)
    regime = 0  # 0 = squeeze, 1 = breakout
    bars_in_regime = 0
    close = 100.0
    closes = np.zeros(n)
    vol_spike = np.zeros(n)
    for t in range(n):
        if bars_in_regime <= 0:
            regime = 1 - regime if rng.random() < 0.5 else regime
            bars_in_regime = rng.integers(8, 25)
        if regime == 0:
            r = rng.normal(0.0, 0.003)
        else:
            direction = 1 if rng.random() < 0.62 else -1
            r = rng.normal(direction * 0.006, 0.011)
            vol_spike[t] = 1.0
        returns[t] = r
        close *= (1 + r)
        closes[t] = close
        bars_in_regime -= 1
    return _ohlcv_from_close(closes, base_vol=1_200_000, vol_spikes=vol_spike,
                              start='2019-01-01', freq='D', rng=rng)


def generate_synthetic(symbol: str, n_bars: int = 1500, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(abs(hash(symbol)) % (2**32) + seed)
    if symbol == 'DEMO_TREND_PULLBACK':
        return _gen_trend_pullback(n_bars, rng)
    if symbol == 'DEMO_MEAN_REVERT':
        return _gen_mean_revert(n_bars, rng)
    if symbol == 'DEMO_VOL_BREAKOUT':
        return _gen_vol_breakout(n_bars, rng)
    raise ValueError(f'unknown synthetic symbol: {symbol}')
