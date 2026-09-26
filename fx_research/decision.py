"""The single decision function used by backtests, walk-forwards, paper and live runs, so every
mode makes (and logs) the same decision from the same inputs."""
from dataclasses import dataclass, field

MIN_VALIDATE_FILLS = 1


@dataclass
class Decision:
    action: str                      # ENTER / SKIP
    reason: str
    gates: dict = field(default_factory=dict)
    signal: dict = field(default_factory=dict)


def decide(strat, bar_age_hours=0.0, max_bar_age_hours=3.0, has_exposure=False, market_open=True,
           require_fit=True):
    """strat is the strategy record from scan.analyse_instrument. Every gate is recorded with its
    value and pass/fail; the first failing gate is the reason for a SKIP."""
    t, v = strat["train_metrics"], strat["validate_metrics"]
    gates = {
        "recommended": {"pass": bool(strat["recommended"]), "train_r": t["mean_r"], "validate_r": v["mean_r"],
                        "validate_fills": v["fills"]},
        "fresh_data": {"pass": bar_age_hours <= max_bar_age_hours, "age_h": round(bar_age_hours, 2),
                       "max_h": max_bar_age_hours},
        "market_open": {"pass": bool(market_open)},
        "no_open_exposure": {"pass": not has_exposure},
    }
    signal = {"recommended": bool(strat["recommended"]), "train": t, "validate": v,
              "params": strat["params"], "fitness": strat["fitness"], "live_hint": strat["live_hint"]}
    reasons = {
        "recommended": "not positive in both train and validate",
        "fresh_data": f"latest bar {bar_age_hours:.1f}h old (> {max_bar_age_hours}h)",
        "market_open": "market closed",
        "no_open_exposure": "already have an open order/position",
    }
    if not require_fit:   # fixed-parameter strategy: nothing was fitted, so no fit gate
        gates.pop("recommended")
    for name, g in gates.items():
        if not g["pass"]:
            return Decision("SKIP", reasons[name], gates, signal)
    return Decision("ENTER", "all gates passed", gates, signal)
