"""Bit-string genetic algorithm that tunes FighterParams.

Same scheme as ../ga.py (bit-string chromosomes, roulette selection, single-point
crossover at rate 0.7, per-bit mutation), ported to Python 3 and applied to
strategy parameters instead of arithmetic expressions. Two elites are carried
over each generation so the best strategy is never lost.
"""
import random
from dataclasses import dataclass

from .backtest import DIRECTION_MODES, FighterParams

CROSSOVER_RATE = 0.7
MUTATION_RATE = 0.02
ELITES = 2

# name -> (bits, low, high, kind). Floats are mapped linearly onto [low, high].
GENES = [
    ("direction_mode", 2, 0, 3, "choice"),
    ("k_atr", 6, 0.05, 1.5, "float"),
    ("sl_atr", 5, 0.3, 3.0, "float"),
    ("tp_atr", 5, 0.3, 5.0, "float"),
    ("ttl_bars", 5, 1, 32, "int"),
    ("reprice_every", 3, 0, 7, "int"),
    ("max_hold_bars", 4, 6, 72, "int"),
]
CHROMO_LENGTH = sum(g[1] for g in GENES)


def decode(bits):
    values, pos = {}, 0
    for name, n, lo, hi, kind in GENES:
        raw = int(bits[pos:pos + n], 2)
        pos += n
        frac = raw / (2 ** n - 1)
        if kind == "choice":
            values[name] = DIRECTION_MODES[raw]
        elif kind == "int":
            values[name] = int(round(lo + frac * (hi - lo)))
        else:
            values[name] = round(lo + frac * (hi - lo), 4)
    return FighterParams(**values)


def random_bits(rng, length=CHROMO_LENGTH):
    return "".join("1" if rng.random() < 0.5 else "0" for _ in range(length))


def crossover(rng, a, b):
    if rng.random() < CROSSOVER_RATE:
        cpos = rng.randrange(1, len(a))
        return a[:cpos] + b[cpos:], b[:cpos] + a[cpos:]
    return a, b


def mutate(rng, bits):
    return "".join(("1" if b == "0" else "0") if rng.random() < MUTATION_RATE else b for b in bits)


def roulette_select(rng, population, weights, total):
    pick = rng.random() * total
    acc = 0.0
    for bits, w in zip(population, weights):
        acc += w
        if acc >= pick:
            return bits
    return population[-1]


@dataclass
class GAResult:
    params: FighterParams
    fitness: float
    generations: int
    evaluations: int
    history: list  # best fitness per generation


def run_ga(score_fn, population_size=40, generations=30, seed=None, seed_params=None):
    """Maximise score_fn(FighterParams) -> float."""
    rng = random.Random(seed)
    cache = {}

    def score(bits):
        if bits not in cache:
            cache[bits] = score_fn(decode(bits))
        return cache[bits]

    population = [random_bits(rng) for _ in range(population_size)]
    if seed_params:
        population[:len(seed_params)] = [encode(p) for p in seed_params]

    history = []
    best_bits, best_fit = None, float("-inf")
    for _ in range(generations):
        fits = [score(b) for b in population]
        ranked = sorted(zip(fits, population), key=lambda t: t[0], reverse=True)
        if ranked[0][0] > best_fit:
            best_fit, best_bits = ranked[0]
        history.append(ranked[0][0])

        # Roulette needs positive weights: shift so the worst chromosome gets a small slice.
        low = min(fits)
        weights = [f - low + 1e-6 for f in fits]
        total = sum(weights)

        nxt = [b for _, b in ranked[:ELITES]]
        while len(nxt) < population_size:
            c1 = roulette_select(rng, population, weights, total)
            c2 = roulette_select(rng, population, weights, total)
            c1, c2 = crossover(rng, c1, c2)
            nxt.extend([mutate(rng, c1), mutate(rng, c2)])
        population = nxt[:population_size]

    return GAResult(decode(best_bits), best_fit, generations, len(cache), history)


def encode(params):
    """Nearest chromosome for a FighterParams (used to seed the population)."""
    bits = ""
    for name, n, lo, hi, kind in GENES:
        v = getattr(params, name)
        if kind == "choice":
            raw = DIRECTION_MODES.index(v)
        else:
            frac = (min(max(v, lo), hi) - lo) / (hi - lo)
            raw = int(round(frac * (2 ** n - 1)))
        bits += format(raw, f"0{n}b")
    return bits
