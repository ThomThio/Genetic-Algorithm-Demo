"""Real round-trip trading cost for a simulated fighter-entry trade: spread plus
overnight swap. This replaces backtest.py's flat "1.5 pip" guess (see
docs/FX_MT4_PLAN.md's "Spread" next-step) with the best available estimate,
while degrading exactly to today's behaviour whenever nothing better exists --
so every number this produces is only ever *more* honest than before, never
less reproducible.

Spread: live bid/ask samples logged by live_runner.py's tick loop (see
sql/fx_research_spread_samples_schema.sql) into fx_research.spread_samples,
if there are enough recent ones; else backtest.spread_for()'s static table,
unchanged. Sample logging only ever runs against a live MT4 terminal, which
this sandbox cannot reach -- the fallback path is what every test here can
actually exercise, and it must stay bit-identical to today's spread_for().

Swap: a measured/overridable price-units-per-rollover table (same shape as
backtest.SPREADS/spread_for), applied once per 17:00-New-York rollover a
trade holds through. Unlisted instruments assume 0 (unmeasured, not free)
rather than inventing a number. fx_research.trades already captures real
swap+commission per closed live position (track_live.py resolve_trade), so
once there's enough closed-trade history, SWAPS should be replaced the same
way spread_for() is being replaced here -- an intentional follow-up, not
part of this fix.
"""
import os

import pandas as pd

from . import data
from .backtest import spread_for

SPREAD_SAMPLES_TABLE = "spread_samples"

# Price units per rollover, positive = cost (same sign convention as SPREADS).
# FXR_SWAPS overrides/extends, e.g. FXR_SWAPS="USDSGD=0.00003,COFFEE.c=0.015".
SWAPS = {}


def swap_for(instrument: str) -> float:
    """Assumed overnight swap in price units: FXR_SWAPS override, else the
    measured table, else 0.0 (not "no cost" -- "not yet measured")."""
    for pair in os.environ.get("FXR_SWAPS", "").split(","):
        name, _, value = pair.partition("=")
        if name.strip() == instrument and value:
            return float(value)
    return SWAPS.get(instrument, 0.0)


def overnight_count(entry_time, exit_time) -> int:
    """Number of 17:00-New-York rollovers strictly between entry and exit,
    reusing data.trading_dates (the same day-boundary convention the rest of
    fx_research already uses for trading-day windows) rather than
    reimplementing the timezone/rollover math here."""
    idx = pd.DatetimeIndex([pd.Timestamp(entry_time), pd.Timestamp(exit_time)])
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    d = data.trading_dates(idx)
    return max(0, int((d[1] - d[0]).days))


def estimate_spread_from_samples(samples: pd.DataFrame, lookback_days: int = 30,
                                   min_samples: int = 30, quantile: float = 0.75,
                                   now=None):
    """samples: DataFrame with tz-aware 'ts' and 'spread' (price units) columns.
    Returns the trailing p`quantile` spread (conservative -- a backtest cost
    estimate should lean pessimistic, not average, so it never overstates an
    edge), or None if fewer than min_samples fall inside the lookback window.
    """
    if samples is None or len(samples) == 0:
        return None
    now = now or pd.Timestamp.now(tz="UTC")
    recent = samples[samples["ts"] >= now - pd.Timedelta(days=lookback_days)]
    if len(recent) < min_samples:
        return None
    return float(recent["spread"].quantile(quantile))


def load_spread_samples(client, schema: str, instrument: str, lookback_days: int = 30) -> pd.DataFrame:
    """Reads fx_research.spread_samples (sql/fx_research_spread_samples_schema.sql),
    written by live_runner.py's tick loop. Returns an empty frame if the table
    doesn't exist yet or nothing has been logged for this instrument -- the
    normal state until the runner has been live for a while, and the only
    state this sandbox (no live MT4 connection) can ever produce."""
    since = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback_days)).isoformat()
    try:
        rows = (client.schema(schema).table(SPREAD_SAMPLES_TABLE).select("ts,spread")
                .eq("instrument", instrument).gte("ts", since).execute().data)
    except Exception:
        return pd.DataFrame(columns=["ts", "spread"])
    if not rows:
        return pd.DataFrame(columns=["ts", "spread"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def spread_for_instrument(instrument: str, client=None, schema: str = "fx_research",
                            lookback_days: int = 30, min_samples: int = 30, quantile: float = 0.75) -> float:
    """Best available spread estimate: live-logged samples if there are
    enough recent ones, else backtest.spread_for()'s static table (identical
    to today's behaviour when client is None or nothing has been logged)."""
    if client is not None:
        samples = load_spread_samples(client, schema, instrument, lookback_days)
        estimate = estimate_spread_from_samples(samples, lookback_days, min_samples, quantile)
        if estimate is not None:
            return estimate
    return spread_for(instrument)


def cost_price_units(instrument: str, entry_time, exit_time, client=None, schema: str = "fx_research") -> float:
    """Total round-trip cost in price units: spread plus swap for every
    rollover the trade holds through. This is what simulate_trade subtracts
    (scaled by 1/risk) at every exit path, in place of the old flat spread."""
    spread = spread_for_instrument(instrument, client=client, schema=schema)
    swap = swap_for(instrument) * overnight_count(entry_time, exit_time)
    return spread + swap
