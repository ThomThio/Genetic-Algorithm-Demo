-- Universal decision log for the trade logic: the SAME tables are written by backtests,
-- walk-forwards, paper and live runs, so results are directly comparable and the strategy's
-- evolution (params, code version, performance over time) can be tracked in one place.
-- PROPOSAL: review, then run in the Supabase SQL editor. Same grants policy as the other
-- fx_research tables: anon can insert/update/select, never delete.

-- One row per run: a backtest, a walk-forward (parent + one child per fold), or a live/paper session.
create table if not exists fx_research.strategy_runs (
    id            uuid primary key default gen_random_uuid(),
    parent_run_id uuid references fx_research.strategy_runs(id) on delete cascade,  -- walk-forward fold -> parent
    fold_index    int,
    mode          text not null check (mode in ('backtest','walkforward','paper','live')),
    strategy      text not null default 'fighter_limit',
    instrument    text not null,
    direction     text,                          -- 'short' / 'long' / 'both'
    timeframe     text not null default 'H1',
    data_source   text,                          -- fx_prices."Source", e.g. FTMO_MT4_demo
    window_days   int,
    params        jsonb not null,                -- engine settings: GA gene values, gates, risk_pct, balance
    code_version  text,                          -- git sha, so results are tied to the code that made them
    train_start   timestamptz, train_end timestamptz,
    test_start    timestamptz, test_end  timestamptz,
    status        text not null default 'running' check (status in ('running','finished','failed','stopped')),
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    summary       jsonb,                         -- FINAL result: trades, mean_r, total_r, win_rate, max_dd_r, sharpe...
    notes         text
);
create index if not exists strategy_runs_ins_mode on fx_research.strategy_runs (instrument, mode, started_at desc);

-- One row per decision the engine takes (every evaluation, including SKIPs): the audit trail.
create table if not exists fx_research.decision_log (
    id           bigint generated always as identity primary key,
    run_id       uuid not null references fx_research.strategy_runs(id) on delete cascade,
    seq          int  not null default 0,        -- order within the same bar
    decided_at   timestamptz not null,           -- market time of the bar (simulated time in backtests)
    logged_at    timestamptz not null default now(),
    instrument   text not null,
    regime       text,
    inputs       jsonb,                          -- what the engine saw: features, analog stats, ATR, bid/ask, spread
    signal       jsonb,                          -- recommended, train/validate R, GA params in force
    gates        jsonb,                          -- each gate with pass/fail and value: staleness, spread, exposure, risk
    action       text not null check (action in ('ENTER','SKIP','REPRICE','CANCEL','EXIT','HOLD')),
    reason       text,                           -- human-readable why (e.g. 'val R < 0', 'bar 16h old')
    order_spec   jsonb,                          -- direction, limit, sl, tp, lots, risk_usd, balance_used
    unique (run_id, instrument, decided_at, seq)
);
create index if not exists decision_log_run_time on fx_research.decision_log (run_id, decided_at);
create index if not exists decision_log_action on fx_research.decision_log (action, decided_at desc);

-- One row per trade, linked to the decision that opened it. Real (live) and simulated trades look the same.
create table if not exists fx_research.trades (
    id           bigint generated always as identity primary key,
    run_id       uuid not null references fx_research.strategy_runs(id) on delete cascade,
    decision_id  bigint references fx_research.decision_log(id),
    instrument   text not null,
    direction    text not null check (direction in ('LONG','SHORT')),
    simulated    boolean not null default true,
    ticket       bigint,                         -- MT4 ticket for live/paper
    status       text not null default 'pending' check (status in ('pending','filled','cancelled','closed')),
    placed_at    timestamptz, filled_at timestamptz, closed_at timestamptz,
    limit_price  double precision, entry double precision, exit double precision,
    sl           double precision, tp double precision,
    lots         double precision, risk_usd double precision,
    spread_cost  double precision,
    r_multiple   double precision,               -- after spread
    pnl_usd      double precision,
    exit_reason  text check (exit_reason in ('tp','sl','max_hold','end_of_data','manual') or exit_reason is null)
);
create index if not exists trades_run on fx_research.trades (run_id, placed_at);

-- Append-only snapshots of a run's results, so the evolution over time is queryable
-- (backtest: one final row; walk-forward: one per fold; live: one per hour/day).
create table if not exists fx_research.run_metrics (
    id          bigint generated always as identity primary key,
    run_id      uuid not null references fx_research.strategy_runs(id) on delete cascade,
    as_of       timestamptz not null,
    final       boolean not null default false,
    decisions   int, entries int, fills int, closed int,
    mean_r      double precision, total_r double precision, win_rate double precision,
    max_dd_r    double precision, fill_rate double precision,
    balance     double precision, equity double precision,
    extra       jsonb,
    created_at  timestamptz not null default now(),
    unique (run_id, as_of)
);

-- Latest results per run, side by side across modes/versions: is a change actually improving things?
create or replace view fx_research.run_leaderboard as
select distinct on (r.id)
       r.id as run_id, r.mode, r.instrument, r.direction, r.timeframe, r.data_source, r.code_version,
       r.started_at, r.status, m.as_of, m.final, m.fills, m.mean_r, m.total_r, m.win_rate, m.max_dd_r, m.equity
from fx_research.strategy_runs r
left join fx_research.run_metrics m on m.run_id = r.id
order by r.id, m.as_of desc;

-- Backtest vs walk-forward vs live for the same setup: the gap between them is the overfitting estimate.
create or replace view fx_research.evolution as
select instrument, direction, mode, code_version, date_trunc('day', started_at) as day,
       count(*) as runs, avg(mean_r) as avg_mean_r, avg(win_rate) as avg_win_rate, sum(fills) as fills
from fx_research.run_leaderboard
group by 1,2,3,4,5;

grant insert, update, select on fx_research.strategy_runs, fx_research.decision_log,
      fx_research.trades, fx_research.run_metrics to anon;
grant select on fx_research.run_leaderboard, fx_research.evolution to anon;
grant all on all tables in schema fx_research to service_role;
