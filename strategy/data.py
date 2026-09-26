"""Data loading: real Supabase OHLCV data (once connected) or a synthetic
demo dataset used to exercise the whole pipeline end-to-end in the meantime.

Supabase usage (once the connector/credentials are available):

    export SUPABASE_URL="https://<project>.supabase.co"
    export SUPABASE_KEY="<anon-or-service-role-key>"
    python3 -m strategy.run_discovery --source supabase --table prices \
        --symbols USDSGD,EURUSD --timeframe H1 --source-name FTMO_MT4_demo

The default table/column names below match this workspace's actual schema
(public.prices): Ccy, Timeframe, Source, Datetime, Open, High, Low, Close,
Volume. Override via the --*-col / --timeframe / --source-name CLI flags (or
the matching kwargs here) if that ever changes.
"""
import os
from typing import List, Optional

import numpy as np
import pandas as pd

REQUIRED_COLS = ['open', 'high', 'low', 'close', 'volume']

# public.prices column names in this workspace's Supabase project.
DEFAULT_TABLE = 'prices'
DEFAULT_SYMBOL_COL = 'Ccy'
DEFAULT_TIMESTAMP_COL = 'Datetime'
DEFAULT_TIMEFRAME_COL = 'Timeframe'
DEFAULT_SOURCE_COL = 'Source'
DEFAULT_PRICE_COL_MAP = {'Open': 'open', 'High': 'high', 'Low': 'low',
                          'Close': 'close', 'Volume': 'volume'}


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


def _supabase_client():
    from supabase import create_client  # imported lazily: optional dependency

    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) must be set '
            'in the environment to load from Supabase.'
        )
    return create_client(url, key)


def load_from_supabase(table: str = DEFAULT_TABLE, symbol: Optional[str] = None,
                        symbol_col: str = DEFAULT_SYMBOL_COL, timestamp_col: str = DEFAULT_TIMESTAMP_COL,
                        timeframe: Optional[str] = None, timeframe_col: str = DEFAULT_TIMEFRAME_COL,
                        source: Optional[str] = None, source_col: str = DEFAULT_SOURCE_COL,
                        col_map: Optional[dict] = None, page_size: int = 5000) -> pd.DataFrame:
    """Pulls one symbol's OHLCV rows from a Supabase table via the
    supabase-py client, paginating with .range() since PostgREST caps
    single-request row counts. Filters on symbol/timeframe/source (any of
    which can be left as None to skip that filter) since a table like
    public.prices holds multiple timeframes and data sources per currency
    pair, not just one series per symbol.
    """
    client = _supabase_client()

    rows = []
    start = 0
    while True:
        q = client.table(table).select('*')
        if symbol is not None:
            q = q.eq(symbol_col, symbol)
        if timeframe is not None:
            q = q.eq(timeframe_col, timeframe)
        if source is not None:
            q = q.eq(source_col, source)
        q = q.order(timestamp_col).range(start, start + page_size - 1)
        resp = q.execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    if not rows:
        raise ValueError(f'no rows returned for table={table!r} symbol={symbol!r} '
                          f'timeframe={timeframe!r} source={source!r}')

    df = pd.DataFrame(rows)
    effective_col_map = {**DEFAULT_PRICE_COL_MAP, **(col_map or {})}
    df = df.rename(columns=effective_col_map)
    df[timestamp_col] = pd.to_datetime(df[timestamp_col])
    df = df.set_index(timestamp_col)
    return _finalize(df)


def list_supabase_symbols(table: str = DEFAULT_TABLE, symbol_col: str = DEFAULT_SYMBOL_COL,
                           timeframe: Optional[str] = None, timeframe_col: str = DEFAULT_TIMEFRAME_COL,
                           source: Optional[str] = None, source_col: str = DEFAULT_SOURCE_COL,
                           sample_rows: int = 20000) -> List[str]:
    """Best-effort distinct symbol listing: PostgREST has no native SELECT
    DISTINCT, so this samples the most recent `sample_rows` rows (optionally
    filtered by timeframe/source) and dedupes client-side. Fine for
    discovering what's in the table; if a symbol trades rarely enough to
    fall outside the sample window, pass it explicitly via --symbols instead.
    """
    client = _supabase_client()
    q = client.table(table).select(symbol_col)
    if timeframe is not None:
        q = q.eq(timeframe_col, timeframe)
    if source is not None:
        q = q.eq(source_col, source)
    resp = q.order('id', desc=True).limit(sample_rows).execute()
    return sorted({row[symbol_col] for row in (resp.data or []) if row.get(symbol_col) is not None})


# --- Local CSV cache ------------------------------------------------------
# A local mirror of Supabase rows, written by strategy/fetch_supabase_csv.py
# and read back here. Lets run_discovery.py run against a snapshot of the
# real data (--source local) without hitting the network every time, and
# keeps working when the Supabase host is unreachable (e.g. a sandboxed
# environment whose network policy blocks it).

DEFAULT_LOCAL_DIR = 'data/prices'


def _local_cache_filename(symbol: str, timeframe: str, source: Optional[str]) -> str:
    safe_source = source if source else 'any'
    return f'{symbol}_{timeframe}_{safe_source}.csv'.replace('/', '-')


def save_to_local_cache(df: pd.DataFrame, symbol: str, timeframe: str, source: Optional[str],
                         cache_dir: str = DEFAULT_LOCAL_DIR) -> str:
    """Writes a DataFrame already in the standard shape (timestamp index,
    lowercase open/high/low/close/volume columns) to the local cache."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, _local_cache_filename(symbol, timeframe, source))
    df.to_csv(path, index_label='timestamp')
    return path


def load_from_local_cache(symbol: str, timeframe: str = 'H1', source: Optional[str] = None,
                           cache_dir: str = DEFAULT_LOCAL_DIR) -> pd.DataFrame:
    path = os.path.join(cache_dir, _local_cache_filename(symbol, timeframe, source))
    if not os.path.exists(path) and source is None:
        # no source filter given: fall back to any single cached file for this symbol+timeframe
        matches = [f for f in os.listdir(cache_dir) if f.startswith(f'{symbol}_{timeframe}_')] \
            if os.path.isdir(cache_dir) else []
        if len(matches) == 1:
            path = os.path.join(cache_dir, matches[0])
    if not os.path.exists(path):
        raise FileNotFoundError(
            f'no local cache file at {path}. Run strategy/fetch_supabase_csv.py first, or check '
            f'--cache-dir/--symbols/--timeframe/--source-name match what was fetched.'
        )
    return load_from_csv(path, timestamp_col='timestamp')


def list_local_symbols(cache_dir: str = DEFAULT_LOCAL_DIR, timeframe: Optional[str] = None,
                        source: Optional[str] = None) -> List[str]:
    if not os.path.isdir(cache_dir):
        return []
    symbols = set()
    for fname in os.listdir(cache_dir):
        if not fname.endswith('.csv'):
            continue
        stem = fname[:-4]
        parts = stem.split('_')
        if len(parts) < 3:
            continue
        sym, tf, src = parts[0], parts[1], '_'.join(parts[2:])
        if timeframe is not None and tf != timeframe:
            continue
        if source is not None and src != source:
            continue
        symbols.add(sym)
    return sorted(symbols)


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
