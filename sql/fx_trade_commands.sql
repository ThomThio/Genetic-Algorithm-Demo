-- Manual entries from the web app. The UI inserts a row (ENTER_LONG / ENTER_SHORT); the runner picks it up within
-- seconds, sizes the fighter entry at the fixed risk % (trading_control.risk_pct) and either places it (runner started
-- with --allow-live AND the trading switch armed) or SIMULATES it (reports the exact order, sends nothing).
-- Commands older than 60 s when the runner sees them are marked expired, never executed late.
-- Run once in the Supabase SQL editor. Same grants policy as the other fx_research tables (no delete).
create table if not exists fx_research.trade_commands (
    id           bigint generated always as identity primary key,
    instrument   text not null,
    action       text not null check (action in ('ENTER_LONG','ENTER_SHORT')),
    status       text not null default 'pending' check (status in ('pending','placed','simulated','rejected','expired')),
    created_at   timestamptz not null default now(),
    handled_at   timestamptz,
    result       jsonb,          -- plan (limit/sl/tp/lots/risk), ticket or the reason it was rejected
    requested_by text
);
create index if not exists trade_commands_pending on fx_research.trade_commands (instrument, status, created_at);
grant insert, update, select on fx_research.trade_commands to anon;
grant all on fx_research.trade_commands to service_role;
