"""Technical indicators used as building blocks for GA-evolved entry conditions.

Every function takes a pandas DataFrame with columns
['open', 'high', 'low', 'close', 'volume'] and returns a pandas Series
aligned to the same index. Kept dependency-free (numpy/pandas only) since
this needs to run in constrained sandboxes without ta-lib.
"""
import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(50.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df['close'].shift(1)
    a = df['high'] - df['low']
    b = (df['high'] - prev_close).abs()
    c = (df['low'] - prev_close).abs()
    return pd.concat([a, b, c], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    line = fast_ema - slow_ema
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = line - sig
    return line, sig, hist


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df['high'].diff()
    down_move = -df['low'].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(df)
    atr_ = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr_.replace(0.0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr_.replace(0.0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan) * 100.0
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean().fillna(0.0)


def stochastic_k(df: pd.DataFrame, period: int = 14) -> pd.Series:
    lowest_low = df['low'].rolling(window=period, min_periods=period).min()
    highest_high = df['high'].rolling(window=period, min_periods=period).max()
    rng = (highest_high - lowest_low).replace(0.0, np.nan)
    return (100.0 * (df['close'] - lowest_low) / rng).fillna(50.0)


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).max()


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).min()


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    base = sma(volume, period).replace(0.0, np.nan)
    return (volume / base).fillna(1.0)


def compute_indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Precompute the indicator set (at a handful of standard periods) once
    per data slice so genome evaluation during the GA loop is just lookups,
    not recomputation. Returned frame is aligned to df.index.
    """
    ind = pd.DataFrame(index=df.index)
    close, volume = df['close'], df['volume']

    for p in (10, 20, 50, 100, 200):
        ind[f'sma_{p}'] = sma(close, p)
    for p in (12, 26):
        ind[f'ema_{p}'] = ema(close, p)
    for p in (7, 14, 21):
        ind[f'rsi_{p}'] = rsi(close, p)

    ind['atr_14'] = atr(df, 14)
    ind['atr_42'] = atr(df, 42)

    macd_line, macd_sig, macd_hist = macd(close)
    ind['macd_line'] = macd_line
    ind['macd_signal'] = macd_sig
    ind['macd_hist'] = macd_hist

    bb_u, bb_m, bb_l = bollinger(close, 20, 2.0)
    ind['bb_upper'] = bb_u
    ind['bb_mid'] = bb_m
    ind['bb_lower'] = bb_l

    ind['adx_14'] = adx(df, 14)

    for p in (14, 21):
        ind[f'stoch_{p}'] = stochastic_k(df, p)

    for p in (10, 20, 55):
        ind[f'hh_{p}'] = rolling_high(df['high'], p)
        ind[f'll_{p}'] = rolling_low(df['low'], p)

    ind['vol_ratio_20'] = volume_ratio(volume, 20)
    return ind
