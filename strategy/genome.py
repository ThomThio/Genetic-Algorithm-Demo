"""Genome encoding for GA-evolved long-only entry rules.

A chromosome is a fixed-length real-valued vector (real-coded GA, values in
[0, 1)) which decodes into a StrategySpec: an AND-combination of up to
N_SLOTS boolean conditions drawn from CONDITION_TEMPLATES, plus a risk model
(ATR-based stop, R-multiple target, max holding period).

Real-coding (rather than the bit-string encoding in ga.py) is used because
the search space here is a mix of categorical choices (which indicator) and
continuous parameters (thresholds, periods) -- a vector of floats with
scaled decoding handles both cleanly while keeping crossover/mutation simple.
"""
import random
from dataclasses import dataclass, field
from typing import List

import numpy as np
import pandas as pd

N_SLOTS = 3
GENES_PER_SLOT = 4  # active, type, param1, param2
RISK_GENES = 3       # stop_atr_mult, target_R, max_hold_bars
GENOME_LENGTH = N_SLOTS * GENES_PER_SLOT + RISK_GENES


def _lerp(x, lo, hi):
    return lo + x * (hi - lo)


def _choice(x, options):
    idx = min(int(x * len(options)), len(options) - 1)
    return options[idx]


def _crosses_above(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def _crosses_above_level(a: pd.Series, level: float) -> pd.Series:
    return (a > level) & (a.shift(1) <= level)


# --- Condition templates -----------------------------------------------
# Each template: key, human name, eval(df, ind, p1_raw, p2_raw) -> bool Series,
# describe(p1_raw, p2_raw) -> str, archetype tag (used for interpretation).

def _t_rsi_oversold(df, ind, p1, p2):
    period = _choice(p1, [7, 14, 21])
    threshold = _lerp(p2, 20.0, 40.0)
    return ind[f'rsi_{period}'] < threshold, {'period': period, 'threshold': round(threshold, 1)}


def _t_rsi_turn_up(df, ind, p1, p2):
    period = _choice(p1, [7, 14, 21])
    threshold = _lerp(p2, 20.0, 40.0)
    return _crosses_above_level(ind[f'rsi_{period}'], threshold), {'period': period, 'threshold': round(threshold, 1)}


def _t_price_above_sma(df, ind, p1, p2):
    period = _choice(p1, [10, 20, 50, 100, 200])
    return df['close'] > ind[f'sma_{period}'], {'period': period}


def _t_sma_fast_above_slow(df, ind, p1, p2):
    fast, slow = _choice(p1, [(10, 50), (20, 100), (50, 200)])
    return ind[f'sma_{fast}'] > ind[f'sma_{slow}'], {'fast': fast, 'slow': slow}


def _t_sma_cross_up(df, ind, p1, p2):
    fast, slow = _choice(p1, [(10, 50), (20, 100), (50, 200)])
    return _crosses_above(ind[f'sma_{fast}'], ind[f'sma_{slow}']), {'fast': fast, 'slow': slow}


def _t_macd_bullish(df, ind, p1, p2):
    return ind['macd_line'] > ind['macd_signal'], {}


def _t_macd_hist_cross_up(df, ind, p1, p2):
    return _crosses_above_level(ind['macd_hist'], 0.0), {}


def _t_bb_lower_touch(df, ind, p1, p2):
    return df['close'] <= ind['bb_lower'], {}


def _t_breakout_n_high(df, ind, p1, p2):
    period = _choice(p1, [10, 20, 55])
    return df['close'] >= ind[f'hh_{period}'], {'period': period}


def _t_adx_trending(df, ind, p1, p2):
    threshold = _lerp(p2, 15.0, 35.0)
    return ind['adx_14'] > threshold, {'threshold': round(threshold, 1)}


def _t_volume_confirm(df, ind, p1, p2):
    mult = _lerp(p2, 1.0, 2.5)
    return ind['vol_ratio_20'] > mult, {'mult': round(mult, 2)}


def _t_stoch_oversold(df, ind, p1, p2):
    period = _choice(p1, [14, 21])
    threshold = _lerp(p2, 10.0, 30.0)
    return ind[f'stoch_{period}'] < threshold, {'period': period, 'threshold': round(threshold, 1)}


def _t_pullback_to_rising_sma(df, ind, p1, p2):
    period = _choice(p1, [20, 50])
    band = _lerp(p2, 0.002, 0.02)
    sma_now = ind[f'sma_{period}']
    sma_rising = sma_now > sma_now.shift(5)
    near = (df['close'] - sma_now).abs() / df['close'] < band
    return near & sma_rising, {'period': period, 'band_pct': round(band * 100, 2)}


def _t_atr_expansion(df, ind, p1, p2):
    mult = _lerp(p2, 1.0, 2.0)
    return (ind['atr_14'] / ind['atr_42']) > mult, {'mult': round(mult, 2)}


CONDITION_TEMPLATES = [
    {'key': 'RSI_OVERSOLD', 'name': 'RSI oversold', 'eval': _t_rsi_oversold,
     'archetype': 'mean_reversion'},
    {'key': 'RSI_TURN_UP', 'name': 'RSI turning up from oversold', 'eval': _t_rsi_turn_up,
     'archetype': 'mean_reversion'},
    {'key': 'PRICE_ABOVE_SMA', 'name': 'price above SMA', 'eval': _t_price_above_sma,
     'archetype': 'trend_filter'},
    {'key': 'SMA_FAST_ABOVE_SLOW', 'name': 'fast SMA above slow SMA', 'eval': _t_sma_fast_above_slow,
     'archetype': 'trend_filter'},
    {'key': 'SMA_CROSS_UP', 'name': 'fast SMA crosses above slow SMA', 'eval': _t_sma_cross_up,
     'archetype': 'trend_trigger'},
    {'key': 'MACD_BULLISH', 'name': 'MACD line above signal', 'eval': _t_macd_bullish,
     'archetype': 'trend_filter'},
    {'key': 'MACD_HIST_CROSS_UP', 'name': 'MACD histogram crosses above zero', 'eval': _t_macd_hist_cross_up,
     'archetype': 'trend_trigger'},
    {'key': 'BB_LOWER_TOUCH', 'name': 'close at/below lower Bollinger Band', 'eval': _t_bb_lower_touch,
     'archetype': 'mean_reversion'},
    {'key': 'BREAKOUT_N_HIGH', 'name': 'new N-bar high breakout', 'eval': _t_breakout_n_high,
     'archetype': 'breakout'},
    {'key': 'ADX_TRENDING', 'name': 'ADX confirms trending regime', 'eval': _t_adx_trending,
     'archetype': 'regime_filter'},
    {'key': 'VOLUME_CONFIRM', 'name': 'volume above its average', 'eval': _t_volume_confirm,
     'archetype': 'confirmation'},
    {'key': 'STOCH_OVERSOLD', 'name': 'stochastic oversold', 'eval': _t_stoch_oversold,
     'archetype': 'mean_reversion'},
    {'key': 'PULLBACK_TO_RISING_SMA', 'name': 'pullback to a rising SMA', 'eval': _t_pullback_to_rising_sma,
     'archetype': 'pullback'},
    {'key': 'ATR_EXPANSION', 'name': 'volatility (ATR) expanding', 'eval': _t_atr_expansion,
     'archetype': 'volatility_expansion'},
]


@dataclass
class Condition:
    key: str
    name: str
    params: dict
    archetype: str

    def label(self) -> str:
        if not self.params:
            return self.name
        parts = ', '.join(f'{k}={v}' for k, v in self.params.items())
        return f'{self.name} ({parts})'


@dataclass
class StrategySpec:
    conditions: List[Condition]
    stop_atr_mult: float
    target_R: float
    max_hold_bars: int
    raw_genome: List[float] = field(default_factory=list)

    def signature(self) -> str:
        cond_sig = '&'.join(sorted(c.key for c in self.conditions))
        return f'{cond_sig}|stop={self.stop_atr_mult:.2f}|R={self.target_R:.2f}|hold={self.max_hold_bars}'

    def rule_text(self) -> str:
        return ' AND '.join(c.label() for c in self.conditions)


def random_genome() -> List[float]:
    return [random.random() for _ in range(GENOME_LENGTH)]


def decode(genome: List[float]) -> StrategySpec:
    picks = []
    for s in range(N_SLOTS):
        base = s * GENES_PER_SLOT
        active, type_sel, p1, p2 = genome[base:base + GENES_PER_SLOT]
        if active <= 0.5 and s > 0:
            # slot 0 is forced active below if nothing else ends up active
            continue
        template = _choice(type_sel, CONDITION_TEMPLATES)
        picks.append((template, p1, p2))

    if not picks:
        _, type_sel, p1, p2 = genome[0:GENES_PER_SLOT]
        picks.append((_choice(type_sel, CONDITION_TEMPLATES), p1, p2))

    # Dedup identical template picks (keep first) to avoid redundant ANDs.
    seen = set()
    uniq = []
    for template, p1, p2 in picks:
        if template['key'] in seen:
            continue
        seen.add(template['key'])
        uniq.append((template, p1, p2))

    risk_base = N_SLOTS * GENES_PER_SLOT
    stop_raw, target_raw, hold_raw = genome[risk_base:risk_base + RISK_GENES]
    stop_atr_mult = round(_lerp(stop_raw, 0.5, 3.0), 2)
    target_R = round(_lerp(target_raw, 1.0, 5.0), 2)
    max_hold_bars = int(_lerp(hold_raw, 5, 60))

    # Params are resolved lazily against real data in materialize_signal();
    # here we just stash the template + raw gene values for later evaluation.
    conditions = [
        Condition(key=template['key'], name=template['name'],
                  params={'__template__': template, '__p1__': p1, '__p2__': p2},
                  archetype=template['archetype'])
        for template, p1, p2 in uniq
    ]
    return StrategySpec(conditions=conditions, stop_atr_mult=stop_atr_mult,
                         target_R=target_R, max_hold_bars=max_hold_bars, raw_genome=list(genome))


def materialize_signal(spec: StrategySpec, df: pd.DataFrame, ind: pd.DataFrame):
    """Evaluate the AND of all conditions against real data, returning
    (signal: bool Series, resolved_conditions: List[Condition] with concrete
    human-readable params filled in).
    """
    signal = pd.Series(True, index=df.index)
    resolved = []
    for c in spec.conditions:
        template = c.params['__template__']
        p1, p2 = c.params['__p1__'], c.params['__p2__']
        series, params = template['eval'](df, ind, p1, p2)
        signal &= series.fillna(False)
        resolved.append(Condition(key=c.key, name=c.name, params=params, archetype=c.archetype))
    return signal, resolved
