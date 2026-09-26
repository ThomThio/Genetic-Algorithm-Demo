"""Turns a decoded strategy (its resolved conditions + risk model + realized
edge stats) into a plain-English rationale for *why* the signal has edge.

This is rule-based (archetype pattern matching), not an LLM call -- it needs
to run standalone inside the GA loop / batch discovery script.
"""
from typing import List

from .genome import Condition
from .fitness import EdgeStats

_ARCHETYPE_STORY = {
    frozenset({'mean_reversion', 'trend_filter'}):
        "This is a pullback-in-uptrend setup: it buys short-term weakness ({mr}) "
        "but only while the higher-timeframe trend filter ({tf}) still favors longs. "
        "The trend filter is what keeps this from being a falling-knife catch -- "
        "the dip is bought only when the broader structure argues it should resolve upward.",
    frozenset({'mean_reversion', 'regime_filter'}):
        "Mean reversion ({mr}) is gated by a regime filter ({rf}), so the strategy only "
        "fades extension when the underlying market is not already trending hard against it -- "
        "avoiding the classic failure mode of mean-reversion entries (buying dips inside a strong downtrend).",
    frozenset({'breakout', 'confirmation'}):
        "This is a confirmed breakout: price clears a recent range extreme ({bo}) and the move is "
        "validated by above-average participation ({cf}). The volume filter is doing the real work here -- "
        "it screens out low-conviction breakouts that tend to fail and reverse.",
    frozenset({'breakout', 'volatility_expansion'}):
        "This captures a volatility-expansion breakout: ATR expanding relative to its own longer average "
        "({ve}) alongside a new range high ({bo}) suggests a squeeze resolving into a directional move, "
        "rather than a breakout into already-elevated, exhausted volatility.",
    frozenset({'trend_filter', 'confirmation'}):
        "Combines a trend-state filter ({tf}) with a volume/participation confirmation gate ({cf}): the "
        "trend condition alone would fire on low-conviction drift too, so requiring above-average "
        "participation screens those periods out, leaving mostly the moves backed by real order flow.",
    frozenset({'trend_trigger', 'regime_filter'}):
        "A trend-following trigger ({tt}) is only acted on when ADX confirms an actively trending regime "
        "({rf}), which should reduce the whipsaw losses that plain moving-average crossovers suffer "
        "in choppy, range-bound conditions.",
    frozenset({'pullback'}):
        "A pullback-to-a-rising-average entry ({pb}): the strategy waits for price to retrace toward a "
        "moving average that is itself still rising, buying the retracement rather than the extension.",
    frozenset({'trend_trigger'}):
        "A pure trend-following trigger ({tt}) with no additional regime filter -- expect more false "
        "signals in sideways markets, offset (if the edge holds) by larger average winners when a real "
        "trend does follow.",
    frozenset({'mean_reversion'}):
        "A standalone mean-reversion entry ({mr}) with no trend filter -- this is the riskiest archetype "
        "here, since it will also fire against strong downtrends. Its edge, if real, likely comes from "
        "a high win rate on quick reversion trades rather than trend participation.",
    frozenset({'trend_filter'}):
        "A standalone trend-state filter ({tf}): it stays 'on' for as long as the condition holds, so it "
        "behaves more like a persistent regime gate than a precisely timed trigger -- expect it to fire "
        "often, in clusters, and for the per-trade edge to come from riding the broader trend rather than "
        "from precise entry timing.",
    frozenset({'breakout'}):
        "A standalone breakout trigger ({bo}) with no volume or volatility confirmation -- it will catch "
        "genuine range expansions but also low-conviction false breakouts that immediately fail, so its "
        "win rate is likely to be well under 50% and the edge (if any) has to come from letting winners run.",
    frozenset({'regime_filter'}):
        "A standalone regime filter ({rf}) used as the entry trigger itself, rather than gating another "
        "signal -- it fires whenever the market becomes trending, without specifying direction or timing, "
        "so a chunk of its trades will catch trend reversals rather than continuations.",
    frozenset({'confirmation'}):
        "A standalone participation filter ({cf}) used as the entry trigger itself, with nothing else "
        "gating it -- above-average volume alone says nothing about direction, so this rule is really "
        "asking 'is something happening' rather than 'should I be long', and its edge (if any) most "
        "likely reflects a general tendency for high-volume bars to precede continuation in this data.",
    frozenset({'volatility_expansion'}):
        "A standalone volatility-expansion trigger ({ve}): it fires whenever ATR is expanding relative to "
        "its own longer average, regardless of direction context -- with no trend or breakout condition "
        "alongside it, roughly half of what it catches should be expansions that go on to fail or reverse.",
}

_LABELS = {
    'mean_reversion': 'mr', 'trend_filter': 'tf', 'trend_trigger': 'tt',
    'breakout': 'bo', 'regime_filter': 'rf', 'confirmation': 'cf',
    'pullback': 'pb', 'volatility_expansion': 've',
}


def _by_archetype(conditions: List[Condition]):
    out = {}
    for c in conditions:
        out.setdefault(c.archetype, []).append(c.label())
    return out


def _edge_shape_sentence(edge: EdgeStats) -> str:
    wr, rr = edge.win_rate, edge.reward_risk
    if wr >= 0.5 and rr >= 1.0:
        shape = ("both win rate and reward:risk pull their weight here -- a robust, "
                 "'high win-rate AND high R:R' combination rather than leaning on one factor")
    elif wr >= 0.55:
        shape = ("the edge is win-rate-driven: it wins more often than it loses even though "
                 "average winners aren't dramatically larger than average losers, "
                 "so consistency is the source of the edge, not big outlier trades")
    elif rr >= 1.5:
        shape = ("the edge is reward:risk-driven: it loses more often than it wins, but winners "
                 "run for multiples of the risk taken on losers, so a handful of large trades "
                 "carry the expectancy")
    else:
        shape = "the edge is thin and close to breakeven on both dimensions -- treat it as marginal"

    return (f"Realized: {edge.n_trades} trades, {wr:.0%} win rate "
            f"(Wilson 95% lower bound {edge.win_rate_lb95:.0%}), reward:risk {rr:.2f}, "
            f"expectancy {edge.expectancy_R:+.2f}R/trade, profit factor {edge.profit_factor:.2f}. "
            f"In this sample {shape}.")


def interpret(conditions: List[Condition], stop_atr_mult: float, target_R: float,
              max_hold_bars: int, edge: EdgeStats) -> str:
    by_arch = _by_archetype(conditions)
    present = frozenset(by_arch.keys())

    story = None
    best_match_size = 0
    for key, template in _ARCHETYPE_STORY.items():
        if key.issubset(present) and len(key) > best_match_size:
            fmt = {_LABELS[a]: '; '.join(labels) for a, labels in by_arch.items()}
            try:
                story = template.format(**fmt)
                best_match_size = len(key)
            except KeyError:
                continue

    if story is None:
        cond_list = '; '.join(c.label() for c in conditions)
        story = (f"Composite rule with no strong archetype match ({cond_list}). "
                 "Its edge, if any, should be treated cautiously and re-validated out-of-sample.")

    risk_sentence = (
        f"Risk model: stop at {stop_atr_mult:.2f}x ATR from entry, target at "
        f"{target_R:.2f}R (i.e. {stop_atr_mult * target_R:.2f}x ATR), max hold {max_hold_bars} bars, "
        f"entries filled at the next bar's open to avoid lookahead."
    )

    return f"{story} {risk_sentence} {_edge_shape_sentence(edge)}"
