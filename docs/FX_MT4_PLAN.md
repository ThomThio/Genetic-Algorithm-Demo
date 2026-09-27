# FX research scanner: plan and how it fits the existing repos

## What each repo does

| Repo | Role | Key pieces reused |
|---|---|---|
| **MT4-TradeSignals** | Live trading. Runs on the Windows VPS next to MT4. | `EACommunicator_API.py` + `ZmqCommunicatorEA` (ZMQ bridge on port 5555), `dataUtil.TradingConn.get_bars` (historic bars), `tradeHelper.place_limit_order` (limit entry `bid − distance` / `ask + distance`, SL/TP as multiples), APScheduler cron jobs in `main.py`, Supabase `public.fx_prices` (H1 bars saved every run) |
| **TradeCoreV2** | Older OANDA system + research. | `to_consolidate.run_fighterEntry` (limit order re-priced every N seconds until it fills), the six regimes in `global_vars.regimes` |
| **Genetic-Algorithm-Demo** (this repo) | Research: match the present against the past and pick the best strategy per pair. | `ga.py` bit-string GA, ported to Python 3 in `fx_research/optimizer.py` |

## What the scanner does (every hour while the FX market is open)

For each pair:

1. **Load ~3 years of H1 bars.** Default source is `public.fx_prices` (Supabase). It can also pull straight from MT4 through the EA (`FXR_BAR_SOURCE=mt4`), or read CSVs.
2. **Build the current window:** the last **7 trading days**. Trading days roll over at 17:00 New York, so weekends are never counted and Sunday-evening bars belong to Monday.
3. **Label its regime**, using the six regimes from TradeCoreV2:
   - `Uptrend` / `Downtrend`: drift z-score (net move ÷ (σ·√n)) above the pair's trend threshold.
   - `Sharp_Uptrend` / `Sharp_Downtrend`: drift z-score in the pair's top ~15% **and** efficient (a straight move, not a choppy one).
   - `Sideways`: no trend, and a variance ratio < 1 (mean-reverting chop).
   - `Random`: no trend, and a variance ratio ≈ 1 (random-walk-like).

   Thresholds are fitted per pair from that pair's own 3-year history, so "sharp" means sharp for that pair.
4. **Search the last 3 years** for the closest 7-trading-day windows. There is one candidate window per trading day, about 780 in total.
   - Distance = 60% path shape (correlation of the volatility-normalised price path) + 40% scalar features (drift, efficiency, variance ratio, volatility, range).
   - Windows in the same regime are ranked first.
   - A window counts only if it and its forward period finish before the current window starts, so the scan never uses data from the period it is judging.
5. **Tune the fighter entry with the GA** on what happened *after* those matches (the next 3 trading days).
   - Genes: direction (follow / fade the drift, or fixed long / short), entry distance in ATR, SL and TP in ATR, time-to-live, re-price interval (0 = static limit, n = re-anchor every n bars, like `run_fighterEntry`), and max hold.
   - Fitness = t-stat of per-trade R after spread. This favours consistency, not a few lucky trades.
   - The oldest 70% of matches train the GA and the newest 30% validate it. `recommended = true` only when both are positive.
   - The current MT4-TradeSignals defaults (0.33 ATR, SL 1.8×, TP 2.8×) are scored on the same trades as a baseline.
6. **Write results** to the Supabase schema `fx_research` (see `sql/fx_research_schema.sql`):
   - `scan_runs`: one row per scan.
   - `regime_snapshots`: current window, regime, features, per-pair thresholds, and how each regime behaved historically.
   - `analog_matches`: the top 25 matched windows with their forward moves.
   - `strategy_results`: GA settings plus train, validation and baseline metrics, and a `live_hint` (direction and distances in price for the current bar).
   - `latest_strategy` (view): the newest result per pair. This is what the live side reads.

## Running it

```bash
pip install -r requirements.txt
psql / Supabase SQL editor  <  sql/fx_research_schema.sql   # then expose "fx_research" under API settings
cp .env.example .env   # fill SUPABASE_URL + SUPABASE_SERVICE_KEY

python -m fx_research.scan --dry-run                     # one scan, JSON to fx_research_output/
python -m fx_research.scheduler --once                   # one scan, writes to Supabase
python -m fx_research.scheduler                          # hourly (Asia/Singapore, minute 5), skips weekends
python -m pytest tests
```

Offline test against the CSVs in MT4-TradeSignals:

```bash
python -m fx_research.scan --source csv --csv-dir ../MT4-TradeSignals/currency_data --instruments GBPUSD,USDJPY --dry-run
```

## Next steps

1. **Backfill 3 years of H1.** `fx_prices` only holds what MT4-TradeSignals has saved since it started. Two ways to fill it:
   - run once with `FXR_BAR_SOURCE=mt4`, after raising MT4 *Tools → Options → Charts → Max bars in history*;
   - import broker history CSVs.
2. **Live side: drafted, not pushed.** `research_strategy.py` for MT4-TradeSignals reads `fx_research.latest_strategy` inside `tradeHelper.place_limit_order`. Behaviour is set with `RESEARCH_MODE`:
   - `params` (default): when the pair is recommended, the result is under 6 hours old, and the scanner agrees on direction, it sends a real `buy_limit` / `sell_limit`. Distances use the scanner's H1 ATR settings, and a thread re-prices the order every `reprice_every` hours and cancels it after `ttl_bars` hours. Otherwise it uses the old entry.
   - `gate`: same, but skips the entry instead of falling back.
   - `off`: the old behaviour.
3. **Spread.** The backtest uses a flat 1.5 pip spread, which is too wide for majors and too tight for crosses. Log the live spread per pair and feed it in.
4. **More strategies.** `strategy` is a column, so volume-profile or RCS (relative currency strength) entries can be scored and compared per regime alongside `fighter_limit`.
