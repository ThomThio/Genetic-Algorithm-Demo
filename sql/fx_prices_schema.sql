-- Historical price bars for this project's own Supabase instance.
--
-- MT4-TradeSignals writes a single-timeframe (H1) public.fx_prices to ITS OWN
-- Supabase project; this project doesn't have that table, so this defines one
-- here with a Timeframe column, so H1 and H4 (or more) can share the table.
-- Column names are quoted/mixed-case to match fx_research/data.py's expectations
-- (Ccy, Datetime, Open, High, Low, Close, Volume) and MT4-TradeSignals' convention.

-- Source is part of the uniqueness key (not just an annotation): the same
-- Ccy/Timeframe/Datetime bar can come from more than one place (e.g. an FTMO
-- demo account vs. a live account vs. a broker CSV export) and prices can
-- genuinely differ between them, so one source must never overwrite another.
create table if not exists public.fx_prices (
    id          bigint generated always as identity primary key,
    "Ccy"       text not null,
    "Timeframe" text not null default 'H1',
    "Source"    text not null,
    "Datetime"  timestamptz not null,
    "Open"      double precision not null,
    "High"      double precision not null,
    "Low"       double precision not null,
    "Close"     double precision not null,
    "Volume"    double precision not null default 0,
    created_at  timestamptz not null default now(),
    unique ("Ccy", "Timeframe", "Source", "Datetime")
);
create index if not exists fx_prices_ccy_tf_src_dt on public.fx_prices ("Ccy", "Timeframe", "Source", "Datetime" desc);

-- Anon (publishable) key writes and reads bars directly, same pattern as
-- MT4-TradeSignals' db.save_mkt_data. No delete grant for anon.
grant usage on schema public to anon, authenticated, service_role;
grant insert, select, update on public.fx_prices to anon, authenticated;
grant all on public.fx_prices to service_role;
grant usage on sequence public.fx_prices_id_seq to anon, authenticated, service_role;
