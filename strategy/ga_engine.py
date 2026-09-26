"""The genetic algorithm loop that evolves long-only entry rules.

Same core GA mechanics as the original ga.py demo (population, fitness-based
selection, crossover, mutation, generational replacement) but real-coded
instead of bit-string, with tournament selection (works with the negative
fitness values low-sample rules get) and elitism, since a losing run here
means "no run found a strategy that clears the trade-count bar" rather than
"never found an exact target".
"""
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from . import genome as genome_mod
from .backtest import run_backtest
from .fitness import compute_edge, EdgeStats, MIN_TRADES_FOR_SIGNAL
from .genome import GENOME_LENGTH, StrategySpec, decode, materialize_signal, random_genome

POP_SIZE = 60
GENERATIONS = 35
TOURNAMENT_SIZE = 3
CROSSOVER_RATE = 0.85
MUTATION_RATE_PER_GENE = 0.12
MUTATION_SIGMA = 0.18
RESET_GENE_RATE = 0.03
ELITE_COUNT = 3
TOP_K_DISTINCT = 8


@dataclass
class Evaluated:
    genome: List[float]
    spec: StrategySpec
    resolved_conditions: list
    edge: EdgeStats
    trades: list
    gen_found: int = -1

    @property
    def fitness(self) -> float:
        # Selection-time-only penalty for rules that haven't fired enough to
        # be statistically meaningful; edge.edge_score itself stays a true,
        # unpenalized estimate for reporting.
        if self.edge.n_trades < MIN_TRADES_FOR_SIGNAL:
            return -10.0 + self.edge.n_trades * 0.05
        return self.edge.edge_score


def _clip01(x: float) -> float:
    return min(0.999999, max(0.0, x))


def evaluate_genome(g: List[float], df: pd.DataFrame, ind: pd.DataFrame) -> Evaluated:
    spec = decode(g)
    signal, resolved = materialize_signal(spec, df, ind)
    trades = run_backtest(df, ind['atr_14'], signal, spec.stop_atr_mult, spec.target_R, spec.max_hold_bars)
    edge = compute_edge(trades)
    return Evaluated(genome=g, spec=spec, resolved_conditions=resolved, edge=edge, trades=trades)


def _tournament_select(pop: List[Evaluated]) -> Evaluated:
    contenders = random.sample(pop, TOURNAMENT_SIZE)
    return max(contenders, key=lambda e: e.fitness)


def _crossover(a: List[float], b: List[float]) -> (List[float], List[float]):
    if random.random() > CROSSOVER_RATE:
        return list(a), list(b)
    c1, c2 = list(a), list(b)
    for i in range(len(a)):
        if random.random() < 0.5:
            c1[i], c2[i] = c2[i], c1[i]
        # arithmetic blend on top, for smoother threshold/period search
        if random.random() < 0.3:
            alpha = random.random()
            v1 = alpha * a[i] + (1 - alpha) * b[i]
            v2 = alpha * b[i] + (1 - alpha) * a[i]
            c1[i], c2[i] = v1, v2
    return c1, c2


def _mutate(g: List[float]) -> List[float]:
    out = list(g)
    for i in range(len(out)):
        if random.random() < RESET_GENE_RATE:
            out[i] = random.random()
        elif random.random() < MUTATION_RATE_PER_GENE:
            out[i] = _clip01(out[i] + random.gauss(0, MUTATION_SIGMA))
    return out


def run_ga(df: pd.DataFrame, ind: pd.DataFrame, pop_size: int = POP_SIZE,
           generations: int = GENERATIONS, seed: Optional[int] = None,
           progress_cb=None) -> List[Evaluated]:
    """Runs the GA against one data slice. Returns the top distinct
    strategies (by decoded rule signature) found across all generations,
    ranked by edge_score, restricted to rules that cleared MIN_TRADES_FOR_SIGNAL.
    """
    if seed is not None:
        random.seed(seed)

    population_genomes = [random_genome() for _ in range(pop_size)]
    best_by_signature: Dict[str, Evaluated] = {}

    for gen in range(generations):
        evaluated = [evaluate_genome(g, df, ind) for g in population_genomes]

        for e in evaluated:
            sig = e.spec.signature()
            prev = best_by_signature.get(sig)
            if prev is None or e.fitness > prev.fitness:
                e.gen_found = gen
                best_by_signature[sig] = e

        evaluated.sort(key=lambda e: e.fitness, reverse=True)

        if progress_cb:
            progress_cb(gen, evaluated[0])

        next_gen = [list(e.genome) for e in evaluated[:ELITE_COUNT]]
        while len(next_gen) < pop_size:
            p1 = _tournament_select(evaluated)
            p2 = _tournament_select(evaluated)
            c1, c2 = _crossover(p1.genome, p2.genome)
            c1, c2 = _mutate(c1), _mutate(c2)
            next_gen.append(c1)
            if len(next_gen) < pop_size:
                next_gen.append(c2)

        population_genomes = next_gen

    qualified = [e for e in best_by_signature.values() if e.edge.n_trades >= MIN_TRADES_FOR_SIGNAL]
    qualified.sort(key=lambda e: e.fitness, reverse=True)
    return qualified[:TOP_K_DISTINCT]
