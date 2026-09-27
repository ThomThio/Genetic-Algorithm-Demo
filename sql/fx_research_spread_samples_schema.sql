-- Logged live bid/ask spread ticks, written by live_runner.py's tick loop (see
-- fx_research/live_runner.py::record_spread_sample and cost_model.py's use of them
-- to replace backtest.spread_for()'s static "1.5 pip" guess with a real, per-pair,
-- time-varying estimate -- see docs/FX_MT4_PLAN.md's "Spread" next-step.
-- Run once in the Supabase SQL editor.

create table if not exists fx_research.spread_samples (
    id          bigint generated always as identity primary key,
    instrument  text not null,
    ts          timestamptz not null,
    bid         double precision not null,
    ask         double precision not null,
    spread      double precision not null              -- ask - bid, price units; denormalized for cheap reads
);
create index if not exists spread_samples_ins_ts on fx_research.spread_samples (instrument, ts desc);

-- Same policy as the other fx_research tables: the runner writes with the service-role key,
-- backtests/scans read with whatever key they have.
grant insert, select on fx_research.spread_samples to service_role;
alter default privileges in schema fx_research grant select on tables to authenticated;

-- Samples accumulate forever otherwise; keep a rolling window (cost_model only ever looks back
-- FXR spread lookback_days=30 by default). Run periodically (e.g. from the same scheduler that
-- runs the hourly scan), or call manually:
--   delete from fx_research.spread_samples where ts < now() - interval '60 days';
