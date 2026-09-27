-- Open (floating) P&L for live trades. Realized P&L is trades.pnl_usd, set when a trade closes
-- (profit + swap + commission from MT4); these columns hold the mark-to-market of a position while it is open.
-- Run once in the Supabase SQL editor.
alter table fx_research.trades
    add column if not exists open_pnl_usd   double precision,   -- floating P&L from OUR polled ticks: (mark - entry) x lots x tick value
    add column if not exists open_r         double precision,   -- same move in R (price move / stop distance)
    add column if not exists mark_price     double precision,   -- polled price used: bid for a long, ask for a short
    add column if not exists last_price     double precision,   -- polled last deal price at the same moment (reference)
    add column if not exists open_costs_usd double precision,   -- swap + commission accrued so far, from MT4 (not price-based)
    add column if not exists pnl_updated_at timestamptz;
