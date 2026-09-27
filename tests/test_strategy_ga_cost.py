"""cost_bps must reach every place strategy/ actually scores a rule -- the GA's own search
(ga_engine.evaluate_genome), the robustness checks (robustness._run_rule), and run_discovery's
in-sample/out-of-sample reporting -- not just be accepted as a parameter somewhere unused.
These tests decode one fixed genome (so entry/exit timing is identical across runs; only the
realized R changes) and check that a non-zero cost_bps produces the same trade count but a
strictly worse (or equal, if never triggered) realized edge than cost_bps=0.0.
"""
from strategy.data import generate_synthetic
from strategy.ga_engine import evaluate_genome
from strategy.genome import decode
from strategy.indicators import compute_indicator_frame
from strategy.robustness import _run_rule
from strategy.fitness import compute_edge


def _fixed_genome():
    # slot 0 (always active): PRICE_ABOVE_SMA(50) -- fires often on a trending series.
    # slots 1-2 inactive (active gene <= 0.5). stop_atr_mult=1.25, target_R=2.2, max_hold=32.
    return [0.9, 0.2, 0.5, 0.0,
            0.1, 0.0, 0.0, 0.0,
            0.1, 0.0, 0.0, 0.0,
            0.3, 0.3, 0.5]


def test_evaluate_genome_cost_bps_reduces_realized_edge_same_trade_count():
    df = generate_synthetic('DEMO_TREND_PULLBACK', n_bars=1500, seed=1)
    ind = compute_indicator_frame(df)
    g = _fixed_genome()

    free = evaluate_genome(g, df, ind, cost_bps=0.0)
    costly = evaluate_genome(g, df, ind, cost_bps=10.0)

    assert free.edge.n_trades > 0, 'fixture rule should fire trades on synthetic data'
    assert free.edge.n_trades == costly.edge.n_trades   # cost doesn't change signal timing
    assert costly.edge.expectancy_R < free.edge.expectancy_R
    assert costly.edge.net_profit_R < free.edge.net_profit_R


def test_robustness_run_rule_threads_cost_bps():
    df = generate_synthetic('DEMO_TREND_PULLBACK', n_bars=1500, seed=1)
    ind = compute_indicator_frame(df)
    spec = decode(_fixed_genome())

    _, free_trades = _run_rule(df, spec, ind, cost_bps=0.0)
    _, costly_trades = _run_rule(df, spec, ind, cost_bps=10.0)

    free_edge, costly_edge = compute_edge(free_trades), compute_edge(costly_trades)
    assert free_edge.n_trades > 0
    assert free_edge.n_trades == costly_edge.n_trades
    assert costly_edge.expectancy_R < free_edge.expectancy_R
