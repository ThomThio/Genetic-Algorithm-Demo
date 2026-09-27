-- Live-ish view: the runner (python -m fx_research.live_runner) writes the latest tick + analysis per
-- instrument here, and the web app (web/) polls it. The web app also writes trading_control to arm/disarm
-- live order placement. Same grants policy as the other fx_research tables: anon insert/update/select, no delete.
-- PROPOSAL: review, then run in the Supabase SQL editor.

create table if not exists fx_research.live_state (
    instrument  text primary key,
    updated_at  timestamptz not null default now(),
    tick        jsonb,        -- {bid, ask, ts}: refreshed every few seconds
    analysis    jsonb,        -- refreshed when a new H1 bar closes: regime, analog paths, plan, decision, gates
    runner      jsonb         -- {mode: paper|live, allow_live, version, started_at}
);

-- The switch behind the "Trading" button. The runner only places orders when it was started with
-- --allow-live AND enabled is true AND armed_until is in the future (arming auto-expires).
create table if not exists fx_research.trading_control (
    instrument  text primary key,
    enabled     boolean not null default false,
    armed_until timestamptz,
    risk_pct    double precision not null default 0.03,
    updated_at  timestamptz not null default now(),
    updated_by  text
);

grant insert, update, select on fx_research.live_state, fx_research.trading_control to anon;
grant all on fx_research.live_state, fx_research.trading_control to service_role;

-- The web app reads bars from public.fx_prices (already readable by anon) and trades/decisions from fx_research.
-- Ensure the reads it needs exist:
grant select on fx_research.trades, fx_research.decision_log, fx_research.strategy_runs to anon;
