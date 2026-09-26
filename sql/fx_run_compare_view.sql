-- Side-by-side comparison of every run's BuildAlpha-style metrics (written by RunLogger.finish for
-- every backtest / walkforward / paper / live run). Run once in the Supabase SQL editor.
create or replace view fx_research.run_compare as
select r.id as run_id, r.started_at, r.mode, r.instrument, r.direction,
       coalesce(r.params->'fixed'->>'direction_mode', r.direction) as strategy_side,
       r.data_source, r.code_version, (r.params->>'spread')::float as spread,
       (r.summary->'edge'->>'n_trades')::int              as n_trades,
       (r.summary->'edge'->>'meets_min_trade_count')::bool as meets_30_trades,
       (r.summary->'edge'->>'win_rate')::float            as win_rate,
       (r.summary->'edge'->>'win_rate_lb95')::float       as win_rate_lb95,
       (r.summary->'edge'->>'profit_factor')::float       as profit_factor,
       (r.summary->'edge'->>'expectancy_R')::float        as expectancy_r,
       (r.summary->'edge'->>'edge_score')::float          as edge_score,
       (r.summary->'edge'->>'net_profit_R')::float        as net_profit_r,
       (r.summary->'edge'->>'max_drawdown_R')::float      as max_drawdown_r,
       (r.summary->'edge'->>'pnl_to_dd_ratio')::float     as pnl_to_dd,
       (r.summary->'edge'->>'sharpe_ratio')::float        as sharpe,
       (r.summary->'in_sample'->>'expectancy_R')::float   as is_expectancy_r,
       (r.summary->'out_of_sample'->>'expectancy_R')::float as oos_expectancy_r,
       (r.summary->'robustness'->'vs_random_distribution'->>'percentile_rank_vs_random')::float as pct_vs_random,
       (r.summary->'robustness'->'noise_test'->>'spread_ratio')::float as noise_spread,
       (r.summary->'robustness'->'permutation_test'->>'percentile_rank_vs_shuffled')::float as pct_vs_shuffled
from fx_research.strategy_runs r
where r.status = 'finished';
grant select on fx_research.run_compare to anon;
