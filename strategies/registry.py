"""
Sélecteur des stratégies à setups : une seule liste, utilisée par le mode analyste (main.py
--analyst), le dashboard (/api/analysis/setups) et le backtest (backtesting/setup_engine.py).

Les stratégies actives sont choisies par SETUP_STRATEGIES (toutes par défaut). Chaque setup passe
par la même séquence (strategies/base.py) : fenêtre d'annonces, taille (1 % par trade), règles de
risque, vérificateur indépendant, puis journal et mémoire des erreurs.
"""

from __future__ import annotations

from typing import Callable, Iterable

from config.settings import SETUP_STRATEGY_NAMES, settings
from strategies.base import BaseStrategy
from strategies.impulse import ImpulseStrategy
from strategies.micro_scalp import MicroScalpStrategy
from strategies.momentum_breakout import MomentumBreakoutStrategy
from strategies.pullback import PullbackStrategy
from strategies.range_rejection import RangeRejectionStrategy
from strategies.top_down import TopDownStrategy

SETUP_STRATEGIES: dict[str, Callable[[], BaseStrategy]] = {
    PullbackStrategy.name: PullbackStrategy,
    RangeRejectionStrategy.name: RangeRejectionStrategy,
    MomentumBreakoutStrategy.name: MomentumBreakoutStrategy,
    ImpulseStrategy.name: ImpulseStrategy,
    MicroScalpStrategy.name: MicroScalpStrategy,
    TopDownStrategy.name: TopDownStrategy,
}
assert tuple(SETUP_STRATEGIES) == SETUP_STRATEGY_NAMES, "config/settings.py : SETUP_STRATEGY_NAMES à mettre à jour"


def strategy_min_ratio(name: str) -> float | None:
    """Ratio net minimal propre à la stratégie `name` pour le vérificateur (None : règle globale)."""
    factory = SETUP_STRATEGIES.get(name)
    return factory().min_net_reward_risk if factory is not None else None


def setup_strategies(names: Iterable[str] | None = None) -> list[BaseStrategy]:
    """Instances des stratégies `names` (par défaut : SETUP_STRATEGIES), dans l'ordre demandé."""
    names = settings.strategy.setup_strategies if names is None else names
    unknown = [n for n in names if n not in SETUP_STRATEGIES]
    if unknown:
        raise ValueError(f"Stratégie(s) inconnue(s) : {', '.join(unknown)} (disponibles : {', '.join(SETUP_STRATEGIES)})")
    return [SETUP_STRATEGIES[n]() for n in names]
