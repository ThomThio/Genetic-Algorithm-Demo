"""Overfitting and stress-testing checks, run on top of a strategy that
already cleared the walk-forward (IS/OOS) gate. These are expensive
(each check reruns full indicator computation + backtest many times), so
run_discovery.py only applies them to already-validated strategies.

1. random_signal_benchmark -- "vs. random distribution": fires the exact
   same risk model (stop/target/hold) off of randomly chosen entry bars,
   matched to the real rule's signal count, many times over. If the real
   rule's edge isn't clearly above what random entry timing achieves with
   the same trade frequency and risk model, the "edge" is likely just the
   risk model (a favorable R:R with enough bars to sometimes hit target).

2. noise_test -- perturbs the OHLC data with small multiplicative noise
   (a proxy for "this exact historical path is not the only one that could
   have happened") many times, and checks how much the edge score varies.
   A wide (p90-p10)/median spread means the rule is fragile to tiny data
   perturbations, i.e. likely curve-fit to specific historical prints.

3. permutation_test -- shuffles the bar-to-bar log-return sequence (so the
   *distribution* of moves is identical but their *order* is randomized),
   rebuilds a synthetic OHLC series from the shuffled path, and reruns the
   same rule. If the rule's edge on the real (ordered) data isn't clearly
   above its edge on shuffled versions, the edge doesn't actually depend on
   genuine temporal/sequential structure (trend, momentum, mean reversion)
   -- it's an artifact of the return distribution alone.
"""
from typing import Optional

import numpy as np
import pandas as pd

from .backtest import run_backtest
from .fitness import compute_edge
from .genome import StrategySpec, materialize_signal
from .indicators import compute_indicator_frame

NOISE_SPREAD_ROBUST_THRESHOLD = 0.5   # (p90-p10)/median on edge_score; standard daily-data threshold
PERCENTILE_SIGNIFICANT = 95.0


def _run_rule(df: pd.DataFrame, spec: StrategySpec, ind: Optional[pd.DataFrame] = None):
    if ind is None:
        ind = compute_indicator_frame(df)
    signal, _ = materialize_signal(spec, df, ind)
    trades = run_backtest(df, ind['atr_14'], signal, spec.stop_atr_mult, spec.target_R, spec.max_hold_bars)
    return signal, trades


def random_signal_benchmark(df: pd.DataFrame, spec: StrategySpec, n_random: int = 200,
                             seed: Optional[int] = None) -> dict:
    """Percentile rank of the real rule's edge_score against n_random
    randomly timed entry series of the same size and risk model.
    """
    rng = np.random.default_rng(seed)
    ind = compute_indicator_frame(df)
    real_signal, real_trades = _run_rule(df, spec, ind)
    real_edge = compute_edge(real_trades)
    n_signals = int(real_signal.sum())

    valid_idx = np.where(ind['atr_14'].notna().to_numpy())[0]
    if n_signals == 0 or len(valid_idx) < n_signals:
        return {'n_random': 0, 'note': 'no signals (or too few valid bars) to benchmark against'}

    random_edge_scores = []
    for _ in range(n_random):
        chosen = rng.choice(valid_idx, size=n_signals, replace=False)
        rand_signal = pd.Series(False, index=df.index)
        rand_signal.iloc[chosen] = True
        trades = run_backtest(df, ind['atr_14'], rand_signal, spec.stop_atr_mult, spec.target_R, spec.max_hold_bars)
        edge = compute_edge(trades)
        if edge.n_trades > 0:
            random_edge_scores.append(edge.edge_score)

    if len(random_edge_scores) < 5:
        return {'n_random': n_random, 'n_usable': len(random_edge_scores),
                'note': 'too few usable random draws to assess'}

    arr = np.array(random_edge_scores)
    percentile_rank = float((arr < real_edge.edge_score).mean() * 100)
    return {
        'n_random': n_random,
        'n_usable': len(random_edge_scores),
        'n_signals_matched': n_signals,
        'real_edge_score': real_edge.edge_score,
        'random_median_edge_score': round(float(np.median(arr)), 4),
        'random_p10_edge_score': round(float(np.percentile(arr, 10)), 4),
        'random_p90_edge_score': round(float(np.percentile(arr, 90)), 4),
        'percentile_rank_vs_random': round(percentile_rank, 1),
        'beats_random_at_95pct': bool(percentile_rank >= PERCENTILE_SIGNIFICANT),
    }


def _perturb_ohlc(df: pd.DataFrame, noise_pct: float, rng: np.random.Generator) -> pd.DataFrame:
    out = df.copy()
    for col in ('open', 'high', 'low', 'close'):
        factors = 1 + rng.normal(0, noise_pct, len(df))
        out[col] = df[col] * factors
    # re-derive high/low so the perturbed bars stay internally consistent
    ohlc = out[['open', 'high', 'low', 'close']]
    out['high'] = ohlc.max(axis=1)
    out['low'] = ohlc.min(axis=1)
    return out


def noise_test(df: pd.DataFrame, spec: StrategySpec, n_variants: int = 100,
                noise_pct: float = 0.05, seed: Optional[int] = None) -> dict:
    """(p90-p10)/median spread of edge_score across noised-OHLC variants.
    Standard threshold: spread < 0.5 on daily-equivalent data counts as robust.
    """
    rng = np.random.default_rng(seed)
    edge_scores = []
    for _ in range(n_variants):
        noisy = _perturb_ohlc(df, noise_pct, rng)
        ind = compute_indicator_frame(noisy)
        _, trades = _run_rule(noisy, spec, ind)
        edge = compute_edge(trades)
        if edge.n_trades > 0:
            edge_scores.append(edge.edge_score)

    if len(edge_scores) < 5:
        return {'n_variants': n_variants, 'n_usable': len(edge_scores),
                'note': 'too few usable variants to assess spread'}

    arr = np.array(edge_scores)
    median = float(np.median(arr))
    p10, p90 = float(np.percentile(arr, 10)), float(np.percentile(arr, 90))
    spread = (p90 - p10) / abs(median) if median != 0 else float('inf')
    return {
        'n_variants': n_variants,
        'n_usable': len(edge_scores),
        'noise_pct': noise_pct,
        'median_edge_score': round(median, 4),
        'p10_edge_score': round(p10, 4),
        'p90_edge_score': round(p90, 4),
        'spread_ratio': round(spread, 3),
        'robust_spread_lt_0_5': bool(spread < NOISE_SPREAD_ROBUST_THRESHOLD),
    }


def permutation_test(df: pd.DataFrame, spec: StrategySpec, n_shuffles: int = 100,
                       seed: Optional[int] = None) -> dict:
    """Shuffles bar-to-bar log returns (same distribution, randomized order),
    rebuilds a synthetic OHLC path, and reruns the rule. Percentile rank of
    the real (ordered) edge_score vs the shuffled distribution -- high means
    the edge depends on genuine sequential structure, not just the return
    distribution.
    """
    rng = np.random.default_rng(seed)
    ind = compute_indicator_frame(df)
    _, real_trades = _run_rule(df, spec, ind)
    real_edge = compute_edge(real_trades)

    close = df['close'].to_numpy()
    log_returns = np.diff(np.log(close))
    rel_range = ((df['high'] - df['low']) / df['close']).to_numpy()
    avg_rel_range = float(np.nanmean(rel_range)) or 0.01
    volume = df['volume'].to_numpy()

    shuffled_edge_scores = []
    for _ in range(n_shuffles):
        shuffled_returns = rng.permutation(log_returns)
        synth_close = np.empty(len(df))
        synth_close[0] = close[0]
        synth_close[1:] = close[0] * np.exp(np.cumsum(shuffled_returns))
        synth_open = np.empty(len(df))
        synth_open[0] = synth_close[0]
        synth_open[1:] = synth_close[:-1]
        intrabar = np.abs(rng.normal(0, avg_rel_range / 4, len(df))) * synth_close
        synth_high = np.maximum(synth_open, synth_close) + intrabar
        synth_low = np.minimum(synth_open, synth_close) - intrabar

        synth_df = pd.DataFrame({'open': synth_open, 'high': synth_high, 'low': synth_low,
                                  'close': synth_close, 'volume': volume}, index=df.index)
        s_ind = compute_indicator_frame(synth_df)
        _, s_trades = _run_rule(synth_df, spec, s_ind)
        s_edge = compute_edge(s_trades)
        if s_edge.n_trades > 0:
            shuffled_edge_scores.append(s_edge.edge_score)

    if len(shuffled_edge_scores) < 5:
        return {'n_shuffles': n_shuffles, 'n_usable': len(shuffled_edge_scores),
                'note': 'too few usable shuffles to assess'}

    arr = np.array(shuffled_edge_scores)
    percentile_rank = float((arr < real_edge.edge_score).mean() * 100)
    return {
        'n_shuffles': n_shuffles,
        'n_usable': len(shuffled_edge_scores),
        'real_edge_score': real_edge.edge_score,
        'shuffled_median_edge_score': round(float(np.median(arr)), 4),
        'shuffled_p90_edge_score': round(float(np.percentile(arr, 90)), 4),
        'percentile_rank_vs_shuffled': round(percentile_rank, 1),
        'depends_on_real_sequence_at_95pct': bool(percentile_rank >= PERCENTILE_SIGNIFICANT),
    }


def run_all(df: pd.DataFrame, spec: StrategySpec, n_random: int = 200, n_noise: int = 100,
             n_shuffles: int = 100, seed: Optional[int] = None) -> dict:
    return {
        'vs_random_distribution': random_signal_benchmark(df, spec, n_random=n_random, seed=seed),
        'noise_test': noise_test(df, spec, n_variants=n_noise, seed=seed),
        'permutation_test': permutation_test(df, spec, n_shuffles=n_shuffles, seed=seed),
    }
