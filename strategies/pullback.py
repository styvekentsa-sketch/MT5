"""
Stratégie A - Pullback de tendance (achat ; la vente est le symétrique exact, calculé sur la
série miroir par strategies/base.py).

Contexte (timeframe de contexte, H1 par défaut, bougies clôturées) :
  1. Tendance haussière confirmée par le détecteur causal (structure + ADX >= 25)
  2. Dernier sommet cassé : pivot confirmé le plus récent dont une clôture ULTÉRIEURE à sa
     confirmation est passée au-dessus
  3. Pas d'invalidation : aucune clôture sous le dernier creux structurel (dernier creux
     confirmé avant la cassure)
  4. Retour sur le sommet cassé : le plus bas de la correction (après le sommet de l'impulsion)
     se trouve dans la bande sommet cassé ± 0.20 ATR
Déclenchement (timeframe de déclenchement, M15 par défaut) :
  5. Dernier sommet de la correction = dernier pivot haut confirmé après le sommet de
     l'impulsion ; cassure confirmée = première clôture au-dessus, dans les 3 dernières bougies
Plan :
  - entrée   : premier Ask après confirmation, refusé s'il s'écarte de plus de 0.20 ATR de la
               clôture de confirmation
  - stop     : creux de correction - b, avec b = max(0.10 ATR, 2 spreads, 1 tick)
  - objectif : TP assurant un ratio net estimé de 3 (gain net / perte prévue, coût = spread)

Statuts : SURVEILLANCE (contexte et retour validés, pas encore de cassure), PRÊT, INVALIDÉ
(entrée trop éloignée), EXPIRÉ (cassure plus ancienne que 3 bougies de déclenchement).
L'ATR utilisé est celui du détecteur : ATR(14) de Wilder disponible avant la dernière bougie.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.regime_detector import WARMUP_BARS, confirmed_pivots, detect_regime
from models.schemas import MarketRegime, SetupStatus, Side, TradingStyle
from strategies.base import (
    ENTRY_MAX_DRIFT_ATR,
    MIN_NET_REWARD_RISK,
    BaseStrategy,
    BuyPlan,
    Quote,
    net_reward_risk,
    protection_margin,
    target_for_ratio,
)

RETEST_BAND_ATR = 0.20
TRIGGER_FRESH_BARS = 3


class PullbackStrategy(BaseStrategy):
    name = "pullback"
    style = TradingStyle.DAY
    bars = 400
    validity_bars = 2

    def __init__(self, context_tf: str = "H1", trigger_tf: str = "M15", style: TradingStyle = TradingStyle.DAY) -> None:
        self.timeframes = (context_tf, trigger_tf)
        self.style = style

    def plan_buy(self, data: dict[str, pd.DataFrame], quote: Quote | None) -> BuyPlan | None:
        ctx = data.get(self.timeframes[0], pd.DataFrame())
        trig = data.get(self.timeframes[1], pd.DataFrame())
        if len(ctx) < WARMUP_BARS or len(trig) < 10:
            return None

        # 1. Tendance haussière confirmée
        reading = detect_regime(ctx)
        if reading.regime != MarketRegime.TREND_UP or not reading.atr:
            return None
        atr = reading.atr
        closes, highs = ctx["close"].to_numpy(dtype=float), ctx["high"].to_numpy(dtype=float)
        pivots = confirmed_pivots(ctx)

        # 2. Dernier sommet cassé (clôture au-dessus après sa confirmation)
        broken = break_pos = None
        for pivot in reversed([p for p in pivots if p.kind == "high"]):
            above = np.nonzero(closes[pivot.confirmed_position + 1:] > pivot.price)[0]
            if above.size:
                broken, break_pos = pivot, pivot.confirmed_position + 1 + int(above[0])
                break
        if broken is None:
            return None

        # 3. Creux structurel non invalidé
        structural = [p for p in pivots if p.kind == "low" and p.confirmed_position < break_pos]
        if not structural or closes[break_pos:].min() < structural[-1].price:
            return None
        structural_low = structural[-1].price

        # 4. Correction après le sommet de l'impulsion, revenue sur le sommet cassé (± 0.20 ATR)
        top_pos = break_pos + int(np.argmax(highs[break_pos:]))
        if top_pos >= len(ctx) - 1:
            return None                                              # pas encore de correction
        correction_start = ctx.index[top_pos]
        trig_correction = trig[trig.index >= correction_start]
        if trig_correction.empty:
            return None
        correction_low = min(float(ctx["low"].iloc[top_pos + 1:].min()), float(trig_correction["low"].min()))
        if not broken.price - RETEST_BAND_ATR * atr <= correction_low <= broken.price + RETEST_BAND_ATR * atr:
            return None

        spread = quote.spread if quote else 0.0
        tick = quote.tick_size if quote else 0.0
        margin = protection_margin(atr, spread, tick)
        stop = correction_low - margin
        levels = [("sommet_cassé", broken.price), ("creux_structurel", structural_low),
                  ("sommet_impulsion", float(highs[top_pos])), ("creux_correction", correction_low)]
        indicators = [("atr", atr), ("marge_b", margin), ("adx", reading.adx or 0.0)]
        # pas de reading.reasons ici : calculés sur la série miroir pour une vente, ils seraient inversés
        context = (f"Tendance {{tendance}} confirmée (structure + ADX {reading.adx:.0f}) ; retour sur le {{sommet}} cassé "
                   f"(± {RETEST_BAND_ATR:g} ATR), {{creux}} structurel intact")

        def plan(status: SetupStatus, entry: float, reason: str, notes: tuple[str, ...] = (),
                 extra: tuple[tuple[str, float], ...] = ()) -> BuyPlan | None:
            if entry <= stop:
                return None                                          # le prix est déjà sous le stop
            cost = spread
            take_profit = target_for_ratio(Side.BUY, entry, stop, MIN_NET_REWARD_RISK, cost)
            return BuyPlan(status, entry, stop, take_profit, net_reward_risk(Side.BUY, entry, stop, take_profit, cost),
                           MarketRegime.TREND_UP, f"{context} ; {reason}", notes, tuple(levels) + extra,
                           tuple(indicators))

        # 5. Déclencheur : cassure du dernier sommet de la correction (timeframe de déclenchement)
        top_time = trig_correction["high"].idxmax()
        correction_highs = [p for p in confirmed_pivots(trig) if p.kind == "high" and p.time > top_time]
        if not correction_highs:
            return plan(SetupStatus.WATCHING, float(ctx["close"].iloc[-1]),
                        "attente d'un {sommet} de correction confirmé sur le timeframe de déclenchement")
        trigger = correction_highs[-1]
        trig_closes = trig["close"].to_numpy(dtype=float)
        above = np.nonzero(trig_closes[trigger.confirmed_position + 1:] > trigger.price)[0]
        trigger_level = (("sommet_correction", trigger.price),)
        if not above.size:
            return plan(SetupStatus.WATCHING, trigger.price + max(tick, 0.0),
                        "attente de la cassure du {sommet} de correction", extra=trigger_level)
        confirm_pos = trigger.confirmed_position + 1 + int(above[0])
        confirm_close = float(trig_closes[confirm_pos])
        extra = trigger_level + (("clôture_confirmation", confirm_close),)
        if len(trig) - 1 - confirm_pos >= TRIGGER_FRESH_BARS:
            return plan(SetupStatus.EXPIRED, confirm_close, "cassure trop ancienne (> "
                        f"{TRIGGER_FRESH_BARS} bougies de déclenchement)", extra=extra)
        if quote is None:
            return plan(SetupStatus.WATCHING, confirm_close, "cassure confirmée, prix exécutable indisponible",
                        extra=extra)
        entry = quote.entry(Side.BUY)
        if abs(entry - confirm_close) > ENTRY_MAX_DRIFT_ATR * atr:
            return plan(SetupStatus.INVALIDATED, entry, "cassure confirmée", extra=extra,
                        notes=(f"entrée refusée : prix à {abs(entry - confirm_close) / atr:.2f} ATR de la clôture "
                               f"de confirmation (maximum {ENTRY_MAX_DRIFT_ATR:g})",))
        return plan(SetupStatus.READY, entry, "cassure confirmée du {sommet} de correction", extra=extra)
