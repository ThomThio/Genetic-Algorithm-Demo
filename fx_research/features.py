"""Window features, regime labels and similarity search.

A window is N trading days of H1 bars. Each window gets:
  * scalar features (drift z-score, efficiency ratio, variance ratio, volatility, range),
  * a shape vector: the volatility-normalised cumulative return path, resampled
    to a fixed length so windows with holidays still line up.

Regimes are the six from TradeCoreV2 (global_vars.regimes). Thresholds are fitted
per pair from its own 3-year history, so "sharp" means sharp for that pair.
"""
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .data import trading_dates

SHAPE_POINTS = 56
SCALAR_KEYS = ["drift_z", "efficiency", "variance_ratio", "log_vol", "range_atr"]


def atr(df, period=20):
    """Mean high-low range, as in MT4-TradeSignals.tradeHelper.calculate_limit_distance."""
    return (df["High"] - df["Low"]).rolling(period, min_periods=1).mean()


def window_features(df):
    close = df["Close"].to_numpy()
    r = np.diff(np.log(close))
    n = len(r)
    if n < 10:
        raise ValueError("window too short")
    sigma = r.std(ddof=1) or 1e-12
    net = r.sum()
    path_len = np.abs(r).sum() or 1e-12
    q = 6
    rq = np.log(close[q:]) - np.log(close[:-q])
    vr = rq.var(ddof=1) / (q * r.var(ddof=1)) if n > 2 * q else 1.0
    hl = (df["High"] - df["Low"]).mean() or 1e-12
    feats = {
        "drift_z": float(net / (sigma * np.sqrt(n))),
        "efficiency": float(abs(net) / path_len),
        "variance_ratio": float(vr),
        "log_vol": float(np.log(sigma)),
        "range_atr": float((df["High"].max() - df["Low"].min()) / hl),
        "net_return": float(np.exp(net) - 1),
    }
    cum = np.concatenate([[0.0], np.cumsum(r)]) / (sigma * np.sqrt(n))
    x_old = np.linspace(0, 1, len(cum))
    shape = np.interp(np.linspace(0, 1, SHAPE_POINTS), x_old, cum)
    return feats, shape


@dataclass
class RegimeThresholds:
    trend_z: float = 1.0        # |drift_z| above this is a trend
    sharp_z: float = 2.2        # |drift_z| above this (and efficient) is a sharp trend
    sharp_efficiency: float = 0.12
    mean_revert_vr: float = 0.9  # variance ratio below this = range-bound (Sideways)

    @classmethod
    def fit(cls, feature_rows):
        """Per-pair thresholds from the distribution of historical windows."""
        if len(feature_rows) < 50:
            return cls()
        z = np.abs([f["drift_z"] for f in feature_rows])
        eff = np.array([f["efficiency"] for f in feature_rows])
        vr = np.array([f["variance_ratio"] for f in feature_rows])
        return cls(
            trend_z=float(np.quantile(z, 0.45)),
            sharp_z=float(np.quantile(z, 0.85)),
            sharp_efficiency=float(np.quantile(eff, 0.6)),
            mean_revert_vr=float(min(np.median(vr), 1.0)),
        )

    def to_dict(self):
        return asdict(self)


def classify(feats, th):
    z = feats["drift_z"]
    if abs(z) >= th.sharp_z and feats["efficiency"] >= th.sharp_efficiency:
        return "Sharp_Uptrend" if z > 0 else "Sharp_Downtrend"
    if abs(z) >= th.trend_z:
        return "Uptrend" if z > 0 else "Downtrend"
    if feats["variance_ratio"] < th.mean_revert_vr:
        return "Sideways"
    return "Random"


@dataclass
class Window:
    start: pd.Timestamp
    end: pd.Timestamp
    end_pos: int          # index (into the bars frame) of the window's last bar
    end_day: int          # index of the window's last trading day
    feats: dict
    shape: np.ndarray
    regime: str = ""


def build_windows(bars, window_days):
    """One window ending at every trading day's close, over all of `bars`."""
    dates = trading_dates(bars.index)
    day_values = dates.unique()
    # position of the first bar of each trading day
    first_pos = np.searchsorted(dates.values, day_values.values, side="left")
    last_pos = np.append(first_pos[1:], len(bars)) - 1
    windows = []
    for i in range(window_days - 1, len(day_values)):
        s, e = first_pos[i - window_days + 1], last_pos[i]
        chunk = bars.iloc[s:e + 1]
        try:
            feats, shape = window_features(chunk)
        except ValueError:
            continue
        windows.append(Window(chunk.index[0], chunk.index[-1], int(e), i, feats, shape))
    return windows, day_values, first_pos, last_pos


def _scalar_matrix(windows):
    return np.array([[w.feats[k] for k in SCALAR_KEYS] for w in windows])


def rank_analogs(current, candidates, top_k, shape_weight=0.6, prefer_same_regime=True):
    """Closest historical windows to `current`. Returns list of (window, distance, shape_corr)."""
    if not candidates:
        return []
    X = _scalar_matrix(candidates)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Xn = (X - mu) / sd
    cn = (np.array([current.feats[k] for k in SCALAR_KEYS]) - mu) / sd
    feat_dist = np.linalg.norm(Xn - cn, axis=1) / np.sqrt(len(SCALAR_KEYS))

    S = np.array([w.shape for w in candidates])
    Sc = S - S.mean(axis=1, keepdims=True)
    cc = current.shape - current.shape.mean()
    denom = np.linalg.norm(Sc, axis=1) * (np.linalg.norm(cc) or 1e-12)
    denom[denom == 0] = 1e-12
    corr = Sc @ cc / denom
    dist = shape_weight * (1 - corr) + (1 - shape_weight) * feat_dist

    order = np.argsort(dist)
    if prefer_same_regime:
        same = [i for i in order if candidates[i].regime == current.regime]
        # Use same-regime windows when there are enough of them, topped up with the rest.
        if len(same) >= max(5, top_k // 2):
            rest = [i for i in order if candidates[i].regime != current.regime]
            order = same + rest
    return [(candidates[i], float(dist[i]), float(corr[i])) for i in order[:top_k]]
