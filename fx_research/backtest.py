"""Bar-by-bar simulation of the fighter entry on H1 bars.

Mirrors the live logic in the old repos:
  * MT4-TradeSignals.tradeHelper.place_limit_order: BUY limit at bid - distance,
    SELL limit at ask + distance, distance scaled by ATR, SL/TP as multiples.
  * TradeCoreV2.to_consolidate.run_fighterEntry: while unfilled, re-anchor the
    limit to the latest price every N intervals until it fills or times out.

Fills are conservative: a limit fills only if the bar trades through it, and when
SL and TP are both inside one bar the SL is assumed to hit first.
"""
from dataclasses import dataclass, asdict

import numpy as np

DIRECTION_MODES = ["follow", "fade", "long", "short"]


@dataclass
class FighterParams:
    direction_mode: str = "follow"   # follow / fade the window's drift, or fixed long / short
    k_atr: float = 0.33              # entry distance from price, in ATR
    sl_atr: float = 0.6              # stop distance from entry, in ATR
    tp_atr: float = 0.92             # target distance from entry, in ATR
    ttl_bars: int = 8                # cancel the order if still unfilled this many bars after the decision
    reprice_every: int = 0           # 0 = static limit; n = re-anchor to price every n bars
    max_hold_bars: int = 48          # exit at close after this many bars in the trade

    def to_dict(self):
        return asdict(self)


# Approximate MT4-TradeSignals defaults (distance 0.33 ATR, SL 1.8x, TP 2.8x distance), for comparison.
MT4_DEFAULT = FighterParams("follow", 0.33, 0.33 * 1.8, 0.33 * 2.8, 8, 0, 48)


def spread_for(instrument):
    """Rough round-trip cost in price units when no broker spread history is available."""
    return 0.015 if "JPY" in instrument else 0.00015


def direction_for(mode, drift):
    if mode == "long":
        return 1
    if mode == "short":
        return -1
    sign = 1 if drift >= 0 else -1
    return sign if mode == "follow" else -sign


def simulate_trade(o, h, l, c, atr_v, start, stop, direction, p, spread):
    """One fighter entry decided at the close of bar `start`; bars up to `stop` (exclusive) usable.

    Returns R multiple (after spread) or None if the order never filled.
    """
    a = atr_v[start]
    if not np.isfinite(a) or a <= 0:
        return None
    dist = p.k_atr * a
    anchor = c[start]
    placed = start
    entry = None
    i = start + 1
    while i < stop:
        limit = anchor - direction * dist
        if entry is None:
            # ttl covers the whole chase, re-prices included.
            if i - start > p.ttl_bars:
                return None
            hit = (l[i] <= limit) if direction == 1 else (h[i] >= limit)
            if hit:
                # Gap through the limit fills at the open (a better price).
                entry = min(o[i], limit) if direction == 1 else max(o[i], limit)
                sl = entry - direction * p.sl_atr * a
                tp = entry + direction * p.tp_atr * a
                fill_bar = i
                if (l[i] <= sl) if direction == 1 else (h[i] >= sl):
                    return (sl - entry) * direction / (p.sl_atr * a) - spread / (p.sl_atr * a)
            elif p.reprice_every and (i - placed) % p.reprice_every == 0:
                anchor, placed = c[i], i
            i += 1
            continue
        stop_hit = (l[i] <= sl) if direction == 1 else (h[i] >= sl)
        tp_hit = (h[i] >= tp) if direction == 1 else (l[i] <= tp)
        risk = p.sl_atr * a
        if stop_hit:
            return (sl - entry) * direction / risk - spread / risk
        if tp_hit:
            return (tp - entry) * direction / risk - spread / risk
        if i - fill_bar >= p.max_hold_bars:
            return (c[i] - entry) * direction / risk - spread / risk
        i += 1
    if entry is None:
        return None
    return (c[stop - 1] - entry) * direction / (p.sl_atr * a) - spread / (p.sl_atr * a)


@dataclass
class Metrics:
    decisions: int = 0
    fills: int = 0
    mean_r: float = 0.0
    std_r: float = 0.0
    total_r: float = 0.0
    win_rate: float = 0.0
    max_drawdown_r: float = 0.0

    @property
    def fill_rate(self):
        return self.fills / self.decisions if self.decisions else 0.0

    def to_dict(self):
        d = asdict(self)
        d["fill_rate"] = self.fill_rate
        return d


def evaluate(arrays, cases, params, spread):
    """Run the strategy over every decision point.

    `cases` is a list of (start_bar, stop_bar, drift) tuples, one per decision.
    """
    o, h, l, c, atr_v = arrays
    rs = []
    for start, stop, drift in cases:
        d = direction_for(params.direction_mode, drift)
        r = simulate_trade(o, h, l, c, atr_v, start, stop, d, params, spread)
        if r is not None:
            rs.append(r)
    m = Metrics(decisions=len(cases), fills=len(rs))
    if rs:
        rs = np.array(rs)
        equity = np.cumsum(rs)
        m.mean_r = float(rs.mean())
        m.std_r = float(rs.std(ddof=1)) if len(rs) > 1 else 0.0
        m.total_r = float(rs.sum())
        m.win_rate = float((rs > 0).mean())
        m.max_drawdown_r = float((np.maximum.accumulate(np.concatenate([[0], equity])) -
                                  np.concatenate([[0], equity])).max())
    return m


def fitness(m, min_fills):
    """t-statistic of the per-trade R, so a few outsized wins can't carry a strategy.

    Too few fills scores below any strategy that trades enough.
    """
    if m.fills < min_fills:
        return -10.0 + m.fills / max(min_fills, 1)
    # Floor the std so near-identical tiny results don't produce huge scores.
    return m.mean_r / max(m.std_r, 0.25) * np.sqrt(m.fills)
