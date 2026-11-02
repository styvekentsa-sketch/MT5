"""
Sortie dynamique d'une position : breakeven puis trailing stop, par paliers de gain latent en R
(R = distance initiale entrée - stop).

  1. breakeven : dès que le meilleur prix atteint +`breakeven_r` R, le stop est remonté au prix
     d'entrée (zéro risque)
  2. trailing  : dès +`activation_r` R, le stop suit le marché selon `mode` :
       - "bars" : plus bas des `bars` dernières bougies clôturées (plus haut pour une vente)
       - "atr"  : meilleur prix - `atr_multiple` x ATR (ATR du setup à l'entrée)
       - "r"    : meilleur prix - `distance_r` x R
  Le stop ne recule jamais. Fonction pure (`next_stop`) : le backtest l'applique après chaque
  bougie clôturée (le nouveau stop vaut pour la bougie suivante) ; le moteur en direct pourra
  l'appliquer de la même façon.

Vente : calcul sur les prix opposés (-p), exactement symétrique.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

TrailingMode = Literal["bars", "atr", "r"]
TRAILING_MODES: tuple[str, ...] = ("bars", "atr", "r")
PRICE_TOLERANCE = 1e-9               # écarts de prix en virgule flottante (1.1015 - 1.1000 < 0.0015)


@dataclass(frozen=True)
class TrailingStop:
    breakeven_r: float = 0.0         # 0 : pas de breakeven
    activation_r: float = 0.0        # 0 : pas de trailing
    mode: TrailingMode = "bars"
    distance_r: float = 1.0          # mode "r"
    atr_multiple: float = 1.5        # mode "atr"
    bars: int = 3                    # mode "bars"

    @property
    def enabled(self) -> bool:
        return self.breakeven_r > 0 or self.activation_r > 0


@dataclass(frozen=True)
class StopUpdate:
    stop: float
    stage: Literal["initial", "breakeven", "trailing"]


def next_stop(side: str, entry: float, initial_stop: float, current_stop: float, best: float,
              recent_extremes: Sequence[float], atr: float | None, config: TrailingStop) -> StopUpdate:
    """
    Nouveau stop après une bougie clôturée.
    `best` : meilleur prix de sortie atteint (plus haut Bid pour un achat, plus bas Ask pour une vente) ;
    `recent_extremes` : plus bas (achat) / plus hauts (vente) des dernières bougies clôturées.
    """
    sign = 1.0 if side == "BUY" else -1.0
    # espace « achat » : une vente est traitée sur les prix opposés
    e, s0, s, b = sign * entry, sign * initial_stop, sign * current_stop, sign * best
    risk = e - s0
    if risk <= 0:
        return StopUpdate(current_stop, "initial")
    gain = b - e
    reached = lambda r: r > 0 and gain >= r * risk * (1 - PRICE_TOLERANCE)  # noqa: E731
    stop = s
    if reached(config.breakeven_r):
        stop = max(stop, e)
    if reached(config.activation_r):
        if config.mode == "bars":
            window = [sign * x for x in recent_extremes][-config.bars:]
            candidate = min(window) if len(window) >= config.bars else None
        elif config.mode == "atr":
            candidate = b - config.atr_multiple * atr if atr else None
        else:
            candidate = b - config.distance_r * risk
        if candidate is not None:
            stop = max(stop, min(candidate, b))           # jamais au-delà du meilleur prix atteint
    tolerance = PRICE_TOLERANCE * max(abs(e), 1.0)
    if abs(stop - e) <= tolerance:
        stage = "breakeven"
    elif stop > s0 + tolerance:
        stage = "trailing"
    else:
        stage = "initial"
    return StopUpdate(sign * stop, stage)
