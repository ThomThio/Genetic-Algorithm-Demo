-- Tables for the FX research scanner, kept in their own schema.
-- Run once in the Supabase SQL editor, then add "fx_research" to
-- Project Settings -> API -> Exposed schemas so supabase-py can reach it.
-- Prices are read from public.fx_prices (written by MT4-TradeSignals); nothing here touches public.

create schema if not exists fx_research;

create table if not exists fx_research.scan_runs (
    id            uuid primary key,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    status        text not null default 'running',   -- running / ok / partial / failed
    instruments   text[],
    settings      jsonb,
    error         text
);

-- Current 7-trading-day window per pair and the regime it was labelled with.
create table if not exists fx_research.regime_snapshots (
    id              bigint generated always as identity primary key,
    run_id          uuid references fx_research.scan_runs(id) on delete cascade,
    instrument      text not null,
    as_of           timestamptz not null,             -- last bar of the window
    window_start    timestamptz not null,
    window_end      timestamptz not null,
    window_days     int not null,
    regime          text not null,
    features        jsonb,                            -- drift_z, efficiency, variance_ratio, ...
    thresholds      jsonb,                            -- per-pair regime thresholds fitted on history
    regime_history  jsonb,                            -- per regime: count, mean forward move (ATR), up share
    history_start   timestamptz,
    history_windows int,
    created_at      timestamptz not null default now()
);
create index if not exists regime_snapshots_ins_asof on fx_research.regime_snapshots (instrument, as_of desc);

-- The closest historical windows (last 3 years) to the current one.
create table if not exists fx_research.analog_matches (
    id            bigint generated always as identity primary key,
    run_id        uuid references fx_research.scan_runs(id) on delete cascade,
    instrument    text not null,
    as_of         timestamptz not null,
    rank          int not null,
    window_start  timestamptz not null,
    window_end    timestamptz not null,
    regime        text not null,
    same_regime   boolean not null,
    distance      double precision,
    shape_corr    double precision,
    fwd_return    double precision,                   -- move over the forward period, fraction
    fwd_move_atr  double precision,                   -- same move in ATR units
    split         text,                               -- train / validate
    created_at    timestamptz not null default now()
);
create index if not exists analog_matches_run on fx_research.analog_matches (run_id, instrument);

-- Best fighter-entry settings the GA found on those matches.
create table if not exists fx_research.strategy_results (
    id               bigint generated always as identity primary key,
    run_id           uuid references fx_research.scan_runs(id) on delete cascade,
    instrument       text not null,
    as_of            timestamptz not null,
    regime           text not null,
    strategy         text not null,                   -- 'fighter_limit'
    params           jsonb not null,                  -- direction_mode, k_atr, sl_atr, tp_atr, ttl_bars, reprice_every, max_hold_bars
    train_metrics    jsonb,
    validate_metrics jsonb,
    all_metrics      jsonb,
    baseline_params  jsonb,                           -- current MT4-TradeSignals defaults, for comparison
    baseline_metrics jsonb,
    fitness          double precision,
    ga               jsonb,
    n_analogs        int,
    recommended      boolean not null default false,  -- positive in train AND validation
    live_hint        jsonb,                           -- direction + distances in price for the live EA
    created_at       timestamptz not null default now()
);
create index if not exists strategy_results_ins_asof on fx_research.strategy_results (instrument, as_of desc);

-- What the live trader should read: latest result per pair.
create or replace view fx_research.latest_strategy as
select distinct on (instrument) *
from fx_research.strategy_results
order by instrument, created_at desc;

-- The scanner writes with the service-role key. Reads for other clients:
grant usage on schema fx_research to anon, authenticated, service_role;
grant select on all tables in schema fx_research to authenticated, service_role;
grant all on all tables in schema fx_research to service_role;
alter default privileges in schema fx_research grant select on tables to authenticated;
alter default privileges in schema fx_research grant all on tables to service_role;
